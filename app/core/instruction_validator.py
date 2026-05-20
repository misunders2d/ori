"""Boot-time ghost-tool + risky-cron validator (slice 6b of the
cron_97f22322 scheduling-fixes work).

Two scans, both fire-and-forget at startup, both log CRITICAL and
admin-alert on hits but never block boot.

* ``validate_agent_tool_refs`` walks the root Agent + every sub-agent
  recursively, scanning each one's ``instruction`` text AND the
  ``.instructions`` body of every Skill attached via SkillToolset.
  Returns a list of unresolved-reference findings — one entry per
  source (agent.instruction OR skill:<name>) per agent.

  The 2026-05-20 cron_97f22322 incident was primed by three ghost-
  tool references in the presentation-skill instruction surface;
  slice 6a stripped them and this validator catches future
  regressions at boot rather than at fire time.

* ``audit_persisted_jobs`` walks the APScheduler job store and flags
  every persisted scheduled job whose ``task_prompt`` trips
  ``app.tasks._match_triggers`` AND whose ``steps`` is empty/None.
  Those jobs are at risk of LLM fabrication on the next fire — the
  Fix 2.2 detector will catch the fabrication once it happens, but
  the audit surfaces the risk pre-emptively so an operator can edit
  the cron OR migrate to a contract before the next scheduled run.

The orchestrator ``run_boot_validation`` is the convenience entry
point ``run_bot.py`` wires in. Scan failures (validator raises,
audit raises, scheduler enumeration raises) all log ERROR AND
admin-alert via ``_alert_admin_safe`` so a quiet validator regression
can't hide a boot-time signal — boot itself still proceeds.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Verb-prefix regex used for BOTH agent.instruction and
# skill.instructions text (slice 6b v3 reviewer revision).
#
# Pattern: ``(call|invoke|run|execute|trigger)(suffixes) `name``` —
# the name must be backticked snake_case with at least one
# underscore. Matches:
#   * "call `execute_sql`"      (genuine tool invocation wording)
#   * "calls `generate_chart`"  (3rd-person variant)
#   * "invoke `foo_bar`"        (alternate verb)
#   * "run `update_record`"     (alternate verb)
# Does NOT match:
#   * "use `kpi_grid` layout"   ("use" deliberately excluded — slide
#                                 layouts + kwarg refs use this verb)
#   * "the `error` field"       (no verb prefix + no underscore)
#   * "see `dictionary_ca`"     (no verb prefix — table-name docs)
#   * "no `drive_upload_file`"  (no verb — disclaimer wording from
#                                 slice 6a; slice 6a's
#                                 test_no_ghost_drive_upload_refs.py
#                                 catches positive-claim regressions
#                                 in the 3 known sites statically)
#
# Reviewer measurement (slice 6b v3): on the live Coordinator tree
# the v2 regex (no underscore requirement, both contexts) produced
# 16 agent-instruction + 60+ skill-instruction false positives. The
# v3 split (broad-for-agent / verb-prefix-for-skill) brought
# skill-side noise to zero but kept 17 agent-instruction false
# positives from BigQueryAgent + ClickUpAgent (both load their
# ``instruction=`` from SKILL.md files documenting table columns,
# kwargs, and ADK callback hook names). Unifying both contexts on
# the verb-prefix shape eliminates the remaining noise without
# missing any genuine tool-invocation wording — the slice 6a
# static test in ``tests/test_no_ghost_drive_upload_refs.py`` is
# the disclaimer-aware static gate for "the existing X" forms.
_NAME_PATTERN = r"([a-z][a-z0-9_]*_[a-z0-9_]+)"

_AGENT_TOOL_REF_RE = re.compile(
    r"\b(?:"
    r"call(?:s|ing|ed)?|invok(?:e|ing|es|ed)?|"
    r"run(?:s|ning)?|execut(?:e|ing|es|ed)?|trigger(?:s|ing|ed)?"
    r")\s+`" + _NAME_PATTERN + "`",
    re.IGNORECASE,
)

# Alias kept for the test suite's separation of concerns and for
# future expansion if the agent vs skill regex diverges again.
_SKILL_TOOL_REF_RE = _AGENT_TOOL_REF_RE


# Identifiers that *look* like tool names but legitimately don't
# resolve to a registered tool. Kept narrow.
#
# - ADK builtins: ``transfer_to_agent`` is provided by ADK, not the
#   local registry.
# - Plan / scratchpad terms: ``complete_step`` / ``get_next_step`` /
#   ``get_plan_status`` / ``create_plan`` / ``seed_plan`` are real
#   tools on most agents but may not be visible from a particular
#   agent's local registry; whitelisting them avoids per-agent noise.
# - Lifecycle macros: ``ACT-XXXXXX`` and family are user-typed
#   approval tokens, not tool calls.
# - Common English boilerplate: ``true`` / ``false`` / ``none``
#   appear as backticked literals in prose ("returns `none` when …").
_WHITELIST: frozenset[str] = frozenset({
    # ADK builtins.
    "transfer_to_agent",
    # Cross-agent planner / scratchpad surface.
    "complete_step", "get_next_step", "get_plan_status", "create_plan",
    "seed_plan",
    # OAuth / connection helpers may not appear on every sub-agent.
    "google_connect", "google_disconnect",
    # Session / lifecycle macros (real tools on Coordinator; whitelisted
    # so sub-agents that only mention them don't false-flag).
    "session_refresh", "update_self", "trigger_rollback",
    # Scheduling notify-dict + ACTIVE_TASKS state-key references that
    # show up in prose ("set ``deliver_to`` to ...").
    "deliver_to", "task_state",
    # Scheduler log-event field names referenced verbatim in prompts
    # (e.g. "quote the verbatim ``response_preview`` field").
    "response_preview",
    # Callback names referenced as documentation (not tools, but real
    # symbols in app/callbacks/). The validator doesn't currently walk
    # the callback registry; future enhancement can drop these in
    # favor of an introspection-based pass.
    "a2a_privacy_guardrail",
})


async def _walk_agent_tree(agent: Any) -> list[Any]:
    """Return every agent reachable from ``agent`` via ``sub_agents``,
    starting with ``agent`` itself. Cycle-safe (dedupes by id())."""
    seen: set[int] = set()
    order: list[Any] = []

    def visit(node: Any) -> None:
        if node is None:
            return
        ident = id(node)
        if ident in seen:
            return
        seen.add(ident)
        order.append(node)
        for sub in getattr(node, "sub_agents", None) or []:
            visit(sub)

    visit(agent)
    return order


async def _collect_registered_tools(agent: Any) -> set[str]:
    """Return every tool name reachable from ``agent`` — walks the
    agent's ``tools`` list (FunctionTool / BaseToolset / plain
    function) AND recurses into ``sub_agents`` so a Coordinator's
    instruction surface can legitimately reference any sub-agent's
    tool by name.

    Resilient by design: a failing toolset (``get_tools`` raises,
    sub-agent attribute missing) is logged + skipped, never
    propagates. A partial registry is better than no boot validation.
    """
    names: set[str] = set()
    for node in await _walk_agent_tree(agent):
        for tool in getattr(node, "tools", None) or []:
            # FunctionTool / Tool — both expose ``name`` directly.
            tool_name = getattr(tool, "name", None)
            if isinstance(tool_name, str) and tool_name:
                names.add(tool_name)
                continue
            # BaseToolset — async get_tools().
            get_tools = getattr(tool, "get_tools", None)
            if callable(get_tools):
                try:
                    subtools = await get_tools()
                except Exception as exc:
                    logger.warning(
                        "instruction_validator: get_tools() failed on "
                        "%r (%s); registry will be partial.",
                        tool, exc,
                    )
                    continue
                for sub in subtools or []:
                    sub_name = getattr(sub, "name", None)
                    if isinstance(sub_name, str) and sub_name:
                        names.add(sub_name)
                continue
            # Plain function — fall back to __name__.
            fn_name = getattr(tool, "__name__", None)
            if isinstance(fn_name, str) and fn_name:
                names.add(fn_name)
    return names


def _resolve_skill_iterable(tool: Any) -> Any:
    """Return an iterable of Skill objects for ``tool`` if it's a
    SkillToolset-shaped object, or None.

    The real ADK ``SkillToolset`` stores skills as a private dict
    ``_skills = {skill.name: skill}`` and exposes a private
    ``_list_skills()`` method
    (``.venv/.../google/adk/tools/skill_toolset.py:700, 790-792``).
    The public ``.skills`` attribute is NOT exposed by the real
    class — slice 6b v2 missed this and only worked against synthetic
    test doubles.

    Access order:
      1. ``_list_skills()`` if callable (preferred — ADK's documented
         internal accessor).
      2. ``_skills.values()`` if ``_skills`` is a dict (fallback for
         future ADK versions that may drop the helper).
      3. Public ``.skills`` (synthetic test shape — kept so the test
         doubles still work, and so a hypothetical future ADK release
         that promotes the attribute keeps working without code change).

    Each branch is wrapped in try/except so a property that raises
    on access (or a dict-like that fails ``values()``) downgrades to
    "no skills here" + WARNING, never propagates.
    """
    # Branch 1: ADK's private accessor.
    list_skills = getattr(tool, "_list_skills", None)
    if callable(list_skills):
        try:
            return list_skills()
        except Exception as exc:
            logger.warning(
                "instruction_validator: %r._list_skills() raised (%s); "
                "skill scan will be partial.",
                tool, exc,
            )
            return None

    # Branch 2: ADK's private dict.
    try:
        _skills = getattr(tool, "_skills", None)
    except Exception as exc:
        logger.warning(
            "instruction_validator: failed to read ``_skills`` on "
            "%r (%s); skill scan will be partial.",
            tool, exc,
        )
        _skills = None
    if isinstance(_skills, dict):
        try:
            return list(_skills.values())
        except Exception as exc:
            logger.warning(
                "instruction_validator: %r._skills.values() raised (%s); "
                "skill scan will be partial.",
                tool, exc,
            )
            return None

    # Branch 3: public attribute (synthetic test shape).
    try:
        public = getattr(tool, "skills", None)
    except Exception as exc:
        logger.warning(
            "instruction_validator: failed to read ``skills`` on "
            "%r (%s); skill scan will be partial.",
            tool, exc,
        )
        return None
    return public


def _agent_skills(agent: Any) -> list[Any]:
    """Return every Skill object attached to ``agent`` via any
    SkillToolset-shaped tool in its ``tools`` list.

    Accepts the real ADK ``SkillToolset`` (private ``_list_skills()``
    accessor / ``_skills`` dict) AND the synthetic ``.skills``
    attribute test shape. Each tool whose attribute layout matches
    one of those forms contributes its skills to the result.

    Failure to enumerate one toolset's skills is logged + skipped —
    a partial skill scan is better than no boot validation.
    """
    found: list[Any] = []
    for tool in getattr(agent, "tools", None) or []:
        skills_iter = _resolve_skill_iterable(tool)
        if skills_iter is None:
            continue
        try:
            for skill in skills_iter:
                if (isinstance(getattr(skill, "name", None), str)
                        and isinstance(getattr(skill, "instructions", None), str)):
                    found.append(skill)
        except Exception as exc:
            logger.warning(
                "instruction_validator: SkillToolset enumeration failed "
                "on %r (%s); skill scan will be partial.",
                tool, exc,
            )
    return found


def _scan_text_for_unresolved(
    text: str,
    registered: set[str],
    *,
    regex: re.Pattern[str] = _AGENT_TOOL_REF_RE,
) -> list[str]:
    """Return the sorted list of backticked snake_case identifiers in
    ``text`` matched by ``regex`` that are neither in ``registered``
    nor on ``_WHITELIST``. Empty list = clean.

    Defaults to ``_AGENT_TOOL_REF_RE`` (broad, for agent.instruction).
    Skill instructions pass ``_SKILL_TOOL_REF_RE`` for the verb-prefix
    variant.
    """
    if not isinstance(text, str) or not text:
        return []
    refs = regex.findall(text)
    if not refs:
        return []
    return sorted({
        r for r in refs
        if r not in registered and r not in _WHITELIST
    })


async def validate_agent_tool_refs(agent: Any) -> list[dict[str, Any]]:
    """Return a list of findings, one per (agent, source) pair whose
    text contains unresolved tool references.

    A finding dict shapes as:
      ``{"agent": str,       # agent.name
         "source": str,      # "instruction" OR "skill:<skill_name>"
         "unresolved": list[str]}``

    Empty list = clean. Callers log + admin-alert on a non-empty
    return; this function does not log on its own.

    Scope (slice 6b reviewer revision):
      * Every agent reachable from ``agent`` via ``sub_agents`` is
        scanned, not just ``agent`` itself.
      * Every Skill attached via ``SkillToolset`` on any of those
        agents has its ``.instructions`` body scanned. The tool
        registry used for resolution is the union across the entire
        tree, so a sub-agent's tool can satisfy a Coordinator's
        skill-instruction reference (and vice versa).
    """
    tree = await _walk_agent_tree(agent)
    registered = await _collect_registered_tools(agent)

    findings: list[dict[str, Any]] = []
    for node in tree:
        agent_name = getattr(node, "name", "<unknown>")

        instruction = getattr(node, "instruction", None)
        unresolved = _scan_text_for_unresolved(instruction or "", registered)
        if unresolved:
            findings.append({
                "agent": agent_name,
                "source": "instruction",
                "unresolved": unresolved,
            })

        for skill in _agent_skills(node):
            skill_name = getattr(skill, "name", "<unknown>")
            skill_text = getattr(skill, "instructions", "") or ""
            # Skill text uses the stricter verb-prefix regex —
            # documented schema/argument backticks dominate the
            # skill instruction surface and would otherwise drown
            # the genuine ghost-tool signal in noise.
            unresolved_s = _scan_text_for_unresolved(
                skill_text, registered, regex=_SKILL_TOOL_REF_RE,
            )
            if unresolved_s:
                findings.append({
                    "agent": agent_name,
                    "source": f"skill:{skill_name}",
                    "unresolved": unresolved_s,
                })

    return findings


def audit_persisted_jobs(scheduler: Any) -> list[dict[str, Any]]:
    """Return a list of risk entries for persisted scheduled jobs
    likely to fabricate on their next fire.

    A job is flagged when:
      * its ``kwargs.task_prompt`` trips
        ``app.tasks._match_triggers`` (Fix 2.2 trigger map), AND
      * its ``kwargs.steps`` is empty/None (no soft enforcement, no
        plan_step_enforcer discipline).

    Returns one dict per flagged job:
      ``{
        "job_id": str,
        "prompt_snippet": str (first 200 chars),
        "matched": list[str]   # matched trigger text per Fix 2.2,
        "kinds":   list[str]   # sorted unique kinds (required_any | unrunnable),
        "remediation": str,
      }``

    Raises on scheduler enumeration failure — the orchestrator catches
    that AND admin-alerts so the failure surfaces in the same
    Rule-13-clean way that a finding does. Per-job introspection
    failures stay local: that single job is skipped with a WARNING
    and the rest of the audit continues.
    """
    # Lazy import to avoid app.tasks ⇄ this module circular import.
    from app.tasks import _match_triggers

    jobs: Iterable[Any] = scheduler.get_jobs()

    flagged: list[dict[str, Any]] = []
    for job in jobs:
        try:
            kw = getattr(job, "kwargs", None) or {}
            prompt = kw.get("task_prompt") or ""
            if not isinstance(prompt, str) or not prompt:
                continue
            steps = kw.get("steps")
            if steps:  # plan-enforced — soft enforcement at minimum
                continue
            matches = _match_triggers(prompt)
            if not matches:
                continue
            flagged.append({
                "job_id": getattr(job, "id", "<unknown>"),
                "prompt_snippet": prompt[:200],
                "matched": [m[0] for m in matches],
                "kinds": sorted({m[2] for m in matches}),
                "remediation": (
                    "edit_scheduled_task(job_id=..., new_steps=[...]) "
                    "for interim soft-enforcement, OR contract_from_existing "
                    "→ tighten spec → contract_dry_run → contract_freeze → "
                    "contract_schedule for the production-grade fix. See "
                    "docs/RUNBOOK.md §12."
                ),
            })
        except Exception:
            logger.warning(
                "audit_persisted_jobs: skipping job %r (introspection "
                "failed).", getattr(job, "id", "<unknown>"),
                exc_info=True,
            )
            continue
    return flagged


async def _alert_admin_safe(text: str) -> None:
    """Best-effort admin alert; Rule 13 cascade — failure of the
    failure-handler itself logs CRITICAL."""
    try:
        from app.contracts.admin_alert import notify_admins
        await notify_admins(text)
    except Exception:
        logger.critical(
            "instruction_validator: admin alert delivery failed — "
            "Rule 13 cascade.", exc_info=True,
        )


def _format_finding(finding: dict[str, Any]) -> str:
    return (
        f"agent=`{finding['agent']}` source=`{finding['source']}` "
        f"unresolved={finding['unresolved']}"
    )


async def run_boot_validation(agent: Any, scheduler: Any) -> None:
    """Boot orchestrator. Run both scans, log CRITICAL + admin-alert
    on hits, log ERROR + admin-alert on scan failures, swallow
    everything else so boot remains robust.

    Wired from ``run_bot.py`` after ``scheduler.start(paused=True)``
    and the runner is constructed. Intentionally fire-and-forget at
    the call site (``asyncio.create_task``) — production tasks must
    not wait on this validation.
    """
    # 1) Agent + skill instruction surfaces.
    try:
        findings = await validate_agent_tool_refs(agent)
    except Exception as exc:
        logger.error(
            "validate_agent_tool_refs raised at boot; instruction "
            "audit skipped: %s",
            exc, exc_info=True,
        )
        await _alert_admin_safe(
            f"Boot validator: validate_agent_tool_refs raised "
            f"({type(exc).__name__}: {exc}). Instruction audit skipped "
            f"this boot — investigate. See docs/RUNBOOK.md §12."
        )
        findings = []

    if findings:
        total_unresolved = sum(len(f["unresolved"]) for f in findings)
        for finding in findings:
            logger.critical(
                "Boot validator: %s. These ghost-tool references will "
                "likely trigger LLM fabrications when the user asks for "
                "them — see docs/RUNBOOK.md §12.",
                _format_finding(finding),
            )
        await _alert_admin_safe(
            f"Boot validator: {total_unresolved} unresolved tool "
            f"reference(s) across {len(findings)} source(s):\n"
            + "\n".join(f"  - {_format_finding(f)}" for f in findings)
            + "\nSee docs/RUNBOOK.md §12."
        )

    # 2) Persisted scheduled-jobs audit.
    try:
        flagged = audit_persisted_jobs(scheduler)
    except Exception as exc:
        logger.error(
            "audit_persisted_jobs raised at boot; persisted-jobs "
            "audit skipped: %s",
            exc, exc_info=True,
        )
        await _alert_admin_safe(
            f"Boot validator: audit_persisted_jobs raised "
            f"({type(exc).__name__}: {exc}). Persisted-jobs audit "
            f"skipped this boot — investigate. See docs/RUNBOOK.md §12."
        )
        flagged = []

    for entry in flagged:
        logger.critical(
            "Boot validator: persisted job %s has a "
            "fabrication-trigger prompt AND steps=None — at risk "
            "of LLM fabrication on next fire. Matched: %s. "
            "Kinds: %s. Remediation: %s",
            entry["job_id"], entry["matched"], entry["kinds"],
            entry["remediation"],
        )
    if flagged:
        await _alert_admin_safe(
            f"Boot validator: {len(flagged)} persisted scheduled "
            f"job(s) at risk of fabrication on next fire: "
            + ", ".join(e["job_id"] for e in flagged)
            + ". See docs/RUNBOOK.md §12."
        )
