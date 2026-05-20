"""Tests for ``app.core.instruction_validator`` (slice 6b of the
cron_97f22322 fabrication-defense work).

Two scans + an orchestrator:

* ``validate_agent_tool_refs(agent)`` — recursive walk over root +
  every sub-agent; per agent scans both ``agent.instruction`` AND
  every Skill attached via SkillToolset (``skill.instructions``).
  Returns finding dicts (one per agent/source combo).
* ``audit_persisted_jobs(scheduler)`` — flags persisted scheduled
  jobs whose ``task_prompt`` trips ``app.tasks._match_triggers``
  AND has empty/None ``steps``. Raises on scheduler enumeration
  failure (orchestrator admin-alerts).
* ``run_boot_validation(agent, scheduler)`` — orchestrates both,
  logs CRITICAL + admin-alerts on hits, logs ERROR + admin-alerts
  on scan failures (Rule 13 — failure surfaces, not just hits),
  swallows scan failures so boot stays robust.

Synthetic agent + scheduler + skill objects only — these tests
don't spin up the real ADK runtime.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.instruction_validator import (
    _AGENT_TOOL_REF_RE,
    _SKILL_TOOL_REF_RE,
    _WHITELIST,
    _agent_skills,
    _collect_registered_tools,
    _resolve_skill_iterable,
    _scan_text_for_unresolved,
    _walk_agent_tree,
    audit_persisted_jobs,
    run_boot_validation,
    validate_agent_tool_refs,
)


# ---------------------------------------------------------------------------
# helpers — synthetic agents, toolsets, skills
# ---------------------------------------------------------------------------


def _function_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _async_toolset(*names: str) -> SimpleNamespace:
    async def get_tools(*_a, **_kw):
        return [_function_tool(n) for n in names]
    return SimpleNamespace(get_tools=get_tools)


def _skill(name: str, instructions: str) -> SimpleNamespace:
    """Synthetic Skill object — exposes .name + .instructions."""
    return SimpleNamespace(name=name, instructions=instructions)


def _skill_toolset(*skills: SimpleNamespace) -> SimpleNamespace:
    """Synthetic SkillToolset (public ``.skills`` attribute) — kept
    for the synthetic-shape branch of ``_resolve_skill_iterable``."""
    return SimpleNamespace(skills=list(skills))


def _real_skill_toolset(*skills: SimpleNamespace) -> SimpleNamespace:
    """Mimics the real ADK SkillToolset shape (``_skills`` dict +
    ``_list_skills()`` private accessor; see
    ``.venv/.../google/adk/tools/skill_toolset.py:700, 790-792``)."""
    by_name = {s.name: s for s in skills}

    def _list_skills():
        return list(by_name.values())

    return SimpleNamespace(_skills=by_name, _list_skills=_list_skills)


def _bare_skills_dict_toolset(*skills: SimpleNamespace) -> SimpleNamespace:
    """Mimics a hypothetical future ADK shape: ``_skills`` dict but
    no ``_list_skills()`` accessor — fallback branch in
    ``_resolve_skill_iterable``."""
    return SimpleNamespace(_skills={s.name: s for s in skills})


def _agent(
    name: str,
    *,
    instruction: str = "",
    tools: list | None = None,
    sub_agents: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        instruction=instruction,
        tools=tools or [],
        sub_agents=sub_agents or [],
    )


# ---------------------------------------------------------------------------
# _walk_agent_tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_walk_agent_tree_visits_root_then_subs():
    leaf = _agent("Leaf")
    mid = _agent("Mid", sub_agents=[leaf])
    root = _agent("Root", sub_agents=[mid])
    tree = await _walk_agent_tree(root)
    names = [a.name for a in tree]
    assert names == ["Root", "Mid", "Leaf"]


@pytest.mark.asyncio
async def test_walk_agent_tree_cycle_safe():
    a = _agent("A")
    b = _agent("B")
    a.sub_agents = [b]
    b.sub_agents = [a]  # cycle
    tree = await _walk_agent_tree(a)
    assert [n.name for n in tree] == ["A", "B"]


@pytest.mark.asyncio
async def test_walk_agent_tree_none_safe():
    assert await _walk_agent_tree(None) == []


# ---------------------------------------------------------------------------
# _collect_registered_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_tools_from_function_tools():
    agent = _agent("A", tools=[_function_tool("foo"), _function_tool("bar")])
    names = await _collect_registered_tools(agent)
    assert names == {"foo", "bar"}


@pytest.mark.asyncio
async def test_collect_tools_from_async_toolset():
    agent = _agent("A", tools=[_async_toolset("execute_sql", "list_tables")])
    names = await _collect_registered_tools(agent)
    assert names == {"execute_sql", "list_tables"}


@pytest.mark.asyncio
async def test_collect_tools_recurses_into_sub_agents():
    sub = _agent("Sub", tools=[_function_tool("sheets_write")])
    parent = _agent("Parent",
                    tools=[_function_tool("execute_sql")],
                    sub_agents=[sub])
    names = await _collect_registered_tools(parent)
    assert names == {"execute_sql", "sheets_write"}


@pytest.mark.asyncio
async def test_collect_tools_toolset_get_tools_failure_logged_not_raised(caplog):
    class BadToolset:
        async def get_tools(self, *_a, **_kw):
            raise RuntimeError("toolset down")
    agent = _agent("A", tools=[_function_tool("ok_tool"), BadToolset()])
    with caplog.at_level("WARNING"):
        names = await _collect_registered_tools(agent)
    assert names == {"ok_tool"}
    assert any(
        "get_tools()" in rec.message and rec.levelname == "WARNING"
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_collect_tools_plain_function_uses_name_attr():
    def my_tool():
        pass
    agent = _agent("A", tools=[my_tool])
    names = await _collect_registered_tools(agent)
    assert "my_tool" in names


# ---------------------------------------------------------------------------
# _resolve_skill_iterable — real ADK shapes + synthetic
# ---------------------------------------------------------------------------


def test_resolve_skill_iterable_real_adk_list_skills():
    """ADK ``SkillToolset`` exposes ``_list_skills()`` returning a
    list of Skill objects. Resolver picks this branch first."""
    s = _skill("foo-skill", "...")
    toolset = _real_skill_toolset(s)
    out = _resolve_skill_iterable(toolset)
    assert list(out) == [s]


def test_resolve_skill_iterable_bare_skills_dict_fallback():
    """Future ADK shape: ``_skills`` dict but no ``_list_skills``."""
    s = _skill("foo-skill", "...")
    toolset = _bare_skills_dict_toolset(s)
    out = _resolve_skill_iterable(toolset)
    assert list(out) == [s]


def test_resolve_skill_iterable_public_skills_attr():
    """Synthetic-test shape — public ``.skills``."""
    s = _skill("foo-skill", "...")
    toolset = _skill_toolset(s)
    out = _resolve_skill_iterable(toolset)
    assert list(out) == [s]


def test_resolve_skill_iterable_none_for_plain_tool():
    """A function tool / plain function has no skill shape — None."""
    assert _resolve_skill_iterable(_function_tool("foo")) is None


def test_resolve_skill_iterable_list_skills_failure_warns_returns_none(caplog):
    class BadToolset:
        def _list_skills(self):
            raise RuntimeError("list_skills boom")
    with caplog.at_level("WARNING"):
        out = _resolve_skill_iterable(BadToolset())
    assert out is None
    assert any("_list_skills()" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _agent_skills
# ---------------------------------------------------------------------------


def test_agent_skills_extracts_from_real_adk_shape():
    """Slice 6b v3 reviewer regression — real ADK ``SkillToolset``
    stores skills on ``_skills`` and exposes ``_list_skills()``.
    v2 of this helper only checked public ``.skills`` and silently
    missed the production shape entirely."""
    s1 = _skill("scheduling-skill", "Call `execute_sql` here.")
    s2 = _skill("approval-skill", "Use `complete_step`.")
    agent = _agent("A", tools=[_real_skill_toolset(s1, s2)])
    out = _agent_skills(agent)
    names = [s.name for s in out]
    assert names == ["scheduling-skill", "approval-skill"]


def test_agent_skills_extracts_from_skill_toolset():
    s1 = _skill("scheduling-skill", "Call `execute_sql` here.")
    s2 = _skill("approval-skill", "Use `complete_step`.")
    agent = _agent("A", tools=[_skill_toolset(s1, s2)])
    out = _agent_skills(agent)
    names = [s.name for s in out]
    assert names == ["scheduling-skill", "approval-skill"]


def test_agent_skills_returns_empty_when_no_skill_toolset():
    agent = _agent("A", tools=[_function_tool("foo"), _async_toolset("x")])
    assert _agent_skills(agent) == []


def test_agent_skills_skips_skill_without_required_attrs():
    """A Skill-shaped object missing .name or .instructions is skipped."""
    bad = SimpleNamespace(name="x")  # no instructions
    agent = _agent("A", tools=[_skill_toolset(bad)])
    assert _agent_skills(agent) == []


def test_agent_skills_handles_skills_enumeration_failure(caplog):
    """If iterating the .skills attribute raises, log + skip."""
    class BadSkillToolset:
        @property
        def skills(self):
            raise RuntimeError("skills boom")
    agent = _agent("A", tools=[BadSkillToolset()])
    with caplog.at_level("WARNING"):
        out = _agent_skills(agent)
    # `skills` attribute access itself raised before the for-loop
    # could start; helper logs WARNING and returns [].
    # Note: since the property raises ON access, _agent_skills's
    # `getattr(tool, "skills", None)` will propagate (no default)
    # only if not caught. Implementation uses try/except around
    # the loop, so a getattr raising won't be caught — fine for
    # this test we check the helper's safety path explicitly.
    assert out == []


# ---------------------------------------------------------------------------
# _scan_text_for_unresolved
# ---------------------------------------------------------------------------


def test_scan_text_returns_empty_for_clean_text():
    assert _scan_text_for_unresolved(
        "Call `execute_sql` then `sheets_write`.",
        registered={"execute_sql", "sheets_write"},
    ) == []


def test_scan_text_flags_unresolved():
    out = _scan_text_for_unresolved(
        "Call `drive_upload_file` after the deck.",
        registered={"drive_list_files"},
    )
    assert out == ["drive_upload_file"]


def test_scan_text_respects_whitelist():
    assert _scan_text_for_unresolved(
        "Use `transfer_to_agent` then `complete_step`.",
        registered=set(),
    ) == []


def test_scan_text_ignores_uppercase():
    assert _scan_text_for_unresolved(
        "Return `HTTP` 200 with `CSV` payload.",
        registered=set(),
    ) == []


def test_scan_text_ignores_single_word_no_underscore():
    """Slice 6b v3: bare-word backticks (`error`, `status`, `id`,
    `range`, `author`) must NOT trigger — single-word literals in
    prose are common and not tool refs."""
    text = (
        "The `error` and `status` fields come from `get_scheduled_task_logs`. "
        "Quote `response_preview` verbatim. Use `range` argument."
    )
    out = _scan_text_for_unresolved(
        text, registered={"get_scheduled_task_logs"},
    )
    # response_preview is on _WHITELIST (state-key reference);
    # error/status/range have no underscore so the regex skips them.
    assert out == []


def test_scan_text_empty_returns_empty():
    assert _scan_text_for_unresolved("", set()) == []
    assert _scan_text_for_unresolved(None, set()) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Agent vs Skill regex contracts
# ---------------------------------------------------------------------------


def test_agent_regex_requires_verb_prefix():
    """Slice 6b v3 final: agent regex unified on verb-prefix shape.
    A bare ``the `X` tool`` reference does NOT trigger — the slice
    6a static test in test_no_ghost_drive_upload_refs.py catches
    declarative ghost refs in the 3 known sites instead."""
    text = "When the user wants Drive, the `drive_upload_file` tool ..."
    assert _AGENT_TOOL_REF_RE.findall(text) == []


def test_agent_regex_catches_verb_prefixed_call():
    """Imperative tool refs DO trigger — same shape as skill regex."""
    text = "Then call `drive_upload_file` separately."
    assert "drive_upload_file" in _AGENT_TOOL_REF_RE.findall(text)


def test_agent_regex_requires_underscore():
    """`error` / `status` / `id` etc. are bare words; even with a
    verb in the sentence, the matched name needs an underscore."""
    text = "Call `error` and call `status`."  # no underscores
    assert _AGENT_TOOL_REF_RE.findall(text) == []


def test_skill_regex_requires_verb_prefix():
    """Skill regex catches `call \\`tool\\`` shape only — bare refs
    in skill schema/field documentation must NOT trigger."""
    schemaish = (
        "The `file_id` field uses `folder_id` as parent; "
        "`spreadsheet_id` is required."
    )
    # No verbs → no matches even though all have underscores.
    assert _SKILL_TOOL_REF_RE.findall(schemaish) == []


def test_skill_regex_catches_verb_prefixed_call():
    text = "If user uploads to Drive, call `drive_upload_file` separately."
    assert _SKILL_TOOL_REF_RE.findall(text) == ["drive_upload_file"]


def test_skill_regex_catches_invoke_run_execute_trigger():
    """All five verbs (call / invoke / run / execute / trigger) +
    their suffixes (-s, -ing, -ed) match."""
    text_invoke = "Then invoke `foo_bar`."
    text_run = "Run `foo_bar` next."
    text_execute = "Execute `foo_bar` once."
    text_trigger = "Trigger `foo_bar` on demand."
    text_calling = "The Coordinator is calling `foo_bar` here."
    text_invokes = "The agent invokes `foo_bar` on transfer."
    for text in (text_invoke, text_run, text_execute, text_trigger,
                 text_calling, text_invokes):
        assert _SKILL_TOOL_REF_RE.findall(text) == ["foo_bar"], (
            f"verb-prefix variant didn't catch: {text!r}"
        )


def test_skill_regex_drops_use_verb_to_avoid_layout_kwarg_noise():
    """Skill prose uses ``use `kpi_grid``` and ``use `chart_path``` —
    schema/layout references, not tool calls. Skill regex
    deliberately excludes ``use`` to avoid this noise."""
    text = "Use `kpi_grid` layout for KPIs. Use `chart_path` arg."
    assert _SKILL_TOOL_REF_RE.findall(text) == []


# ---------------------------------------------------------------------------
# validate_agent_tool_refs — recursive + skill scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_clean_agent_returns_empty():
    agent = _agent(
        "Coord",
        instruction="Call `execute_sql` then `sheets_write`.",
        tools=[_function_tool("execute_sql"), _function_tool("sheets_write")],
    )
    assert await validate_agent_tool_refs(agent) == []


@pytest.mark.asyncio
async def test_validate_flags_root_instruction_ghost():
    agent = _agent(
        "Coord",
        instruction="If user wants Drive, call `drive_upload_file`.",
        tools=[_function_tool("drive_list_files")],
    )
    findings = await validate_agent_tool_refs(agent)
    assert len(findings) == 1
    assert findings[0]["agent"] == "Coord"
    assert findings[0]["source"] == "instruction"
    assert findings[0]["unresolved"] == ["drive_upload_file"]


@pytest.mark.asyncio
async def test_validate_flags_subagent_instruction_recursively():
    sub = _agent(
        "AmazonWorkspaceAgent",
        instruction="Call `drive_upload_file` separately AFTER the deck.",
        tools=[_function_tool("drive_list_files")],
    )
    root = _agent(
        "Coord",
        instruction="Delegate Drive work to AmazonWorkspaceAgent.",
        tools=[],
        sub_agents=[sub],
    )
    findings = await validate_agent_tool_refs(root)
    by_agent = {f["agent"]: f for f in findings}
    assert "AmazonWorkspaceAgent" in by_agent
    assert by_agent["AmazonWorkspaceAgent"]["source"] == "instruction"
    assert "drive_upload_file" in by_agent["AmazonWorkspaceAgent"]["unresolved"]


@pytest.mark.asyncio
async def test_validate_flags_attached_skill_instructions():
    """The 2026-05-20 incident root cause — ghost references lived in
    skills/presentation-skill/SKILL.md. Validator must walk attached
    skills, not just agent.instruction."""
    bad_skill = _skill(
        "presentation-skill",
        "If the user says upload to Drive, call `drive_upload_file` "
        "separately AFTER `generate_presentation`.",
    )
    agent = _agent(
        "AmazonWorkspaceAgent",
        instruction="See skill for Drive details.",
        tools=[_skill_toolset(bad_skill), _function_tool("generate_presentation")],
    )
    findings = await validate_agent_tool_refs(agent)
    by_source = {f["source"]: f for f in findings}
    assert "skill:presentation-skill" in by_source
    assert "drive_upload_file" in by_source["skill:presentation-skill"]["unresolved"]


@pytest.mark.asyncio
async def test_validate_subagent_skill_resolves_against_root_tools():
    """Union registry — a sub-agent's skill instruction can reference
    a tool that's only registered on the root, and vice versa."""
    skill_for_sub = _skill(
        "scheduling-skill",
        "Use `execute_sql` when reading the cron schedule.",
    )
    sub = _agent("Sub", tools=[_skill_toolset(skill_for_sub)])
    root = _agent(
        "Coord",
        instruction="",
        tools=[_function_tool("execute_sql")],  # registered at root
        sub_agents=[sub],
    )
    assert await validate_agent_tool_refs(root) == []


@pytest.mark.asyncio
async def test_validate_returns_multiple_findings_for_multiple_sources():
    """Both root.instruction AND a skill on a sub-agent flag — get
    one finding entry per source."""
    bad_skill = _skill("bad-skill", "Call `ghost_b`.")
    sub = _agent("Sub", tools=[_skill_toolset(bad_skill)])
    root = _agent(
        "Coord",
        instruction="Call `ghost_a`.",
        tools=[],
        sub_agents=[sub],
    )
    findings = await validate_agent_tool_refs(root)
    sources = sorted({f["source"] for f in findings})
    assert sources == ["instruction", "skill:bad-skill"]
    agents = sorted({f["agent"] for f in findings})
    assert agents == ["Coord", "Sub"]


@pytest.mark.asyncio
async def test_validate_empty_instruction_no_skill_returns_empty():
    agent = _agent("Empty", instruction="", tools=[_function_tool("x")])
    assert await validate_agent_tool_refs(agent) == []


# ---------------------------------------------------------------------------
# audit_persisted_jobs
# ---------------------------------------------------------------------------


def _job(job_id: str, prompt: str, steps: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=job_id, kwargs={"task_prompt": prompt, "steps": steps})


def _scheduler(jobs: list) -> MagicMock:
    sched = MagicMock()
    sched.get_jobs.return_value = jobs
    return sched


def test_audit_flags_trigger_prompt_with_no_steps():
    sched = _scheduler([
        _job("cron_97f22322",
             "Query `reports.business_report_asin` for the top 50 ASINs.",
             steps=None),
    ])
    flagged = audit_persisted_jobs(sched)
    assert len(flagged) == 1
    assert flagged[0]["job_id"] == "cron_97f22322"
    assert "required_any" in flagged[0]["kinds"]
    assert "RUNBOOK" in flagged[0]["remediation"]


def test_audit_ignores_job_with_steps():
    sched = _scheduler([
        _job("cron_safe",
             "Query `reports.business_report_asin`.",
             steps=["Step 1", "Step 2"]),
    ])
    assert audit_persisted_jobs(sched) == []


def test_audit_ignores_job_with_no_triggers():
    sched = _scheduler([
        _job("cron_remind", "Remind me about the call at 4pm", steps=None),
    ])
    assert audit_persisted_jobs(sched) == []


def test_audit_flags_drive_upload_unrunnable_prompt():
    sched = _scheduler([
        _job("cron_x", "Upload the CSV to Google Drive.", steps=None),
    ])
    flagged = audit_persisted_jobs(sched)
    assert len(flagged) == 1
    assert "unrunnable" in flagged[0]["kinds"]


def test_audit_raises_on_scheduler_enumeration_failure():
    """v6b reviewer revision: audit no longer silently swallows
    scheduler failure — the orchestrator catches the raise and
    admin-alerts."""
    sched = MagicMock()
    sched.get_jobs.side_effect = RuntimeError("DB down")
    with pytest.raises(RuntimeError, match="DB down"):
        audit_persisted_jobs(sched)


def test_audit_skips_individual_bad_jobs_without_failing_all(caplog):
    bad = MagicMock()
    type(bad).kwargs = property(
        lambda _self: (_ for _ in ()).throw(RuntimeError("broken kwargs"))
    )
    bad.id = "cron_broken"
    sched = _scheduler([
        bad,
        _job("cron_ok",
             "Query `reports.business_report_asin`.", steps=None),
    ])
    with caplog.at_level("WARNING"):
        flagged = audit_persisted_jobs(sched)
    job_ids = [f["job_id"] for f in flagged]
    assert "cron_ok" in job_ids
    assert "cron_broken" not in job_ids


def test_audit_handles_empty_kwargs():
    j = SimpleNamespace(id="cron_legacy", kwargs=None)
    sched = _scheduler([j])
    assert audit_persisted_jobs(sched) == []


# ---------------------------------------------------------------------------
# run_boot_validation orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_boot_validation_clean_no_critical_no_admin_alert(caplog):
    agent = _agent(
        "Clean",
        instruction="Use `execute_sql`.",
        tools=[_function_tool("execute_sql")],
    )
    sched = _scheduler([])
    with caplog.at_level("CRITICAL"):
        with patch("app.contracts.admin_alert.notify_admins",
                   new=AsyncMock()) as mock_notify:
            await run_boot_validation(agent, sched)
    assert not mock_notify.called
    assert not any(
        rec.levelname == "CRITICAL" and "Boot validator" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_run_boot_validation_ghost_tool_alerts_admin(caplog):
    agent = _agent(
        "Coord",
        instruction="Call `drive_upload_file` after the deck.",
        tools=[],
    )
    sched = _scheduler([])
    with caplog.at_level("CRITICAL"):
        with patch("app.contracts.admin_alert.notify_admins",
                   new=AsyncMock()) as mock_notify:
            await run_boot_validation(agent, sched)
    assert mock_notify.called
    alert_text = mock_notify.call_args.args[0]
    assert "drive_upload_file" in alert_text
    assert "Coord" in alert_text
    assert any(
        rec.levelname == "CRITICAL"
        and "drive_upload_file" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_run_boot_validation_skill_ghost_alerts_admin(caplog):
    bad_skill = _skill("p-skill", "Call `drive_upload_file` after deck.")
    agent = _agent("A", instruction="", tools=[_skill_toolset(bad_skill)])
    sched = _scheduler([])
    with caplog.at_level("CRITICAL"):
        with patch("app.contracts.admin_alert.notify_admins",
                   new=AsyncMock()) as mock_notify:
            await run_boot_validation(agent, sched)
    assert mock_notify.called
    alert_text = mock_notify.call_args.args[0]
    assert "skill:p-skill" in alert_text
    assert "drive_upload_file" in alert_text


@pytest.mark.asyncio
async def test_run_boot_validation_risky_job_alerts_admin(caplog):
    agent = _agent("Clean", instruction="", tools=[])
    sched = _scheduler([
        _job("cron_97f22322",
             "Query `reports.business_report_asin`.", steps=None),
    ])
    with caplog.at_level("CRITICAL"):
        with patch("app.contracts.admin_alert.notify_admins",
                   new=AsyncMock()) as mock_notify:
            await run_boot_validation(agent, sched)
    assert mock_notify.called
    alert_text = mock_notify.call_args.args[0]
    assert "cron_97f22322" in alert_text


@pytest.mark.asyncio
async def test_run_boot_validation_validator_failure_admin_alerts(caplog):
    """v6b reviewer revision: validator failure now alerts admin in
    addition to logging ERROR."""
    agent = _agent("X", instruction="`execute_sql`")
    sched = _scheduler([])
    with patch(
        "app.core.instruction_validator.validate_agent_tool_refs",
        new=AsyncMock(side_effect=RuntimeError("validator broken")),
    ):
        with caplog.at_level("ERROR"):
            with patch("app.contracts.admin_alert.notify_admins",
                       new=AsyncMock()) as mock_notify:
                await run_boot_validation(agent, sched)
    assert any(
        "instruction audit skipped" in rec.message.lower()
        for rec in caplog.records
    )
    # Admin alert fired for the failure itself.
    assert mock_notify.called
    alert_text = mock_notify.call_args.args[0]
    assert "validate_agent_tool_refs raised" in alert_text


@pytest.mark.asyncio
async def test_run_boot_validation_audit_failure_admin_alerts(caplog):
    """v6b reviewer revision: scheduler enumeration failure now
    propagates from audit_persisted_jobs; orchestrator catches +
    admin-alerts."""
    agent = _agent("X", instruction="", tools=[])
    sched = MagicMock()
    sched.get_jobs.side_effect = RuntimeError("scheduler boom")
    with caplog.at_level("ERROR"):
        with patch("app.contracts.admin_alert.notify_admins",
                   new=AsyncMock()) as mock_notify:
            await run_boot_validation(agent, sched)
    assert any(
        "persisted-jobs audit skipped" in rec.message.lower()
        for rec in caplog.records
    )
    assert mock_notify.called
    alert_text = mock_notify.call_args.args[0]
    assert "audit_persisted_jobs raised" in alert_text


@pytest.mark.asyncio
async def test_run_boot_validation_admin_alert_failure_does_not_propagate(caplog):
    agent = _agent("Coord", instruction="Call `ghost_tool_x`.", tools=[])
    sched = _scheduler([])
    with patch("app.contracts.admin_alert.notify_admins",
               new=AsyncMock(side_effect=RuntimeError("Slack down"))):
        with caplog.at_level("CRITICAL"):
            await run_boot_validation(agent, sched)
    assert any(
        rec.levelname == "CRITICAL"
        and "admin alert delivery failed" in rec.message.lower()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# WHITELIST sanity
# ---------------------------------------------------------------------------


def test_whitelist_contains_known_adk_builtin():
    assert "transfer_to_agent" in _WHITELIST


def test_whitelist_does_not_contain_drive_upload_file():
    assert "drive_upload_file" not in _WHITELIST


# ---------------------------------------------------------------------------
# Live-tree smoke test — the real Coordinator + all sub-agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_coordinator_tree_has_zero_ghost_findings():
    """Slice 6b v3 reviewer regression: at boot the validator must
    be high-signal — zero false positives on the production agent
    tree, otherwise the CRITICAL-log + admin-alert posture becomes
    noise and operators learn to ignore the validator.

    This test loads the real ``app.agent.root_agent`` and asserts
    ``validate_agent_tool_refs`` returns []. Future regressions
    (ghost tool refs in an agent instruction OR a verb-prefixed
    ghost-tool call in any attached SKILL.md) will fail this test
    and force the author to either add the tool, fix the wording,
    or whitelist the reference deliberately.
    """
    from app.agent import root_agent

    findings = await validate_agent_tool_refs(root_agent)
    assert findings == [], (
        f"Live Coordinator tree has unresolved ghost-tool references — "
        f"either register the tool, edit the wording, or expand the "
        f"validator whitelist deliberately. Findings:\n"
        + "\n".join(f"  {f}" for f in findings)
    )
