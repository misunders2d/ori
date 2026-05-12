"""Contract-pipeline tools — the agent-facing surface for authoring,
dry-running, freezing, scheduling, and revising scheduled-task
contracts.

UX: the agent (in conversation with the user) DRAFTS a contract — a
``dict`` matching the ``Contract`` pydantic shape (or its serialised
form). Then it calls:

    1. ``contract_dry_run(spec)``           — simulate one fire, see
                                              rendered emit args.
    2. ``contract_freeze(spec)``            — persist + hash. Returns
                                              ``{id, version, hash}``.
    3. ``contract_schedule(id)``            — wire to APScheduler.

To revise: ``contract_revise(id, new_spec)`` — same as freeze but
records ``parent_hash``.

To migrate an existing legacy job: ``contract_from_existing(job_id)``
returns a draft spec that approximates the old job. The agent reviews,
tweaks, runs dry-run, freezes, schedules, then deletes the legacy
job — all on demand.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

from app.contracts.executor import (
    list_contract_jobs,
    schedule_contract as _schedule,
    unschedule_contract as _unschedule,
)
from app.contracts.schema import Contract, EnforcementMode
from app.contracts.store import (
    ContractNotFound,
    contract_store,
)
from app.contracts.worker import execute_contract

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_spec(spec: Any) -> Contract:
    """Build a Contract from either a dict or a JSON string. The agent
    can pass whichever is easier; we accept both."""
    if isinstance(spec, str):
        import json

        spec = json.loads(spec)
    if not isinstance(spec, dict):
        raise ValueError(
            "contract spec must be a dict (or JSON string parseable to one). "
            f"Got {type(spec).__name__}."
        )
    contract = Contract.model_validate(spec)
    # Semantic check on top of Pydantic shape: every adapter/gate/loader
    # the contract references must actually be registered. Catches the
    # 2026-05-12 class of bug where contracts referenced
    # `slack_post_message` (the tool name) instead of `slack_post` (the
    # registered emit adapter). Pydantic accepts any string here;
    # validation makes the bot fail-loud at authoring time.
    from app.contracts.validation import validate_against_registries
    validate_against_registries(contract)
    return contract


# ---------------------------------------------------------------------------
# Author tools
# ---------------------------------------------------------------------------


async def contract_draft_validate(spec: dict, tool_context: ToolContext) -> dict:
    """Validate a freshly-drafted contract spec WITHOUT persisting.

    Use this immediately after composing a contract dict to catch
    schema / typing errors before you ask the user to review. Returns
    ``{"status": "ok", "summary": {...}}`` on success, or
    ``{"status": "error", "errors": [...]}`` on failure.

    Args:
        spec (dict): The contract dict you authored. Must conform to
            ``app.contracts.schema.Contract`` (id, description, author,
            trigger, emit, plus optional inputs / reasoning / acceptance /
            on_failure / enforcement).

    Returns:
        dict
    """
    try:
        c = _coerce_spec(spec)
        return {
            "status": "ok",
            "summary": {
                "id": c.id,
                "description": c.description,
                "trigger": c.trigger.model_dump(),
                "input_count": len(c.inputs),
                "reasoning_step_count": len(c.reasoning),
                "emit_count": len(c.emit),
                "enforcement": c.enforcement.value,
            },
        }
    except Exception as e:
        return {"status": "error", "errors": [str(e)]}


async def contract_dry_run(
    spec: dict,
    tool_context: ToolContext,
    mock_inputs: Optional[dict] = None,
) -> dict:
    """Simulate one fire of a contract WITHOUT applying any side
    effects. Loaders run normally (unless overridden via
    ``mock_inputs``), reasoning steps invoke the LLM, and emit
    adapters are skipped — their rendered args come back in
    ``__emit_results__`` so the user can see exactly what would post.

    Use this BEFORE ``contract_freeze`` to verify the contract
    produces the right output shape on a real fire.

    Args:
        spec (dict): The contract dict to simulate. Need not be
            frozen yet — dry-run accepts drafts.
        mock_inputs (dict | None): Optional ``{input_id: value}`` map
            that short-circuits specified loaders with fixed values
            (handy for testing reasoning without hitting real APIs).

    Returns:
        dict: ``__status__``, plus the full state dict (inputs,
        reasoning outputs, emit_results with rendered args).
    """
    c = _coerce_spec(spec)
    # Dry-run accepts unfrozen contracts — we hash them in-flight just
    # so worker's integrity check has something to verify against, but
    # do NOT persist.
    c = c.with_fresh_hash()
    contract_store_singleton_temp = contract_store  # alias

    # Inject the unsaved contract into the store's in-process index so
    # ``execute_contract``'s hash-verify can find it. We undo on exit.
    contract_store_singleton_temp._locks  # touch attr for lint
    versions_before = contract_store_singleton_temp.list_versions(c.id)
    try:
        contract_store_singleton_temp.freeze(c)
    except Exception:
        pass

    try:
        return await execute_contract(c, dry_run=True, mock_inputs=mock_inputs)
    finally:
        # If we minted a new version just for the dry-run and the user
        # hasn't seen it yet, leave it. The freeze step is idempotent;
        # the next real freeze will pick up from this version.
        _ = versions_before


async def contract_freeze(spec: dict, tool_context: ToolContext) -> dict:
    """Persist a contract to disk, hash it, and record it in the
    version index. Returns ``{id, version, hash}``.

    Refuses to freeze a contract whose ``enforcement`` is not STRICT —
    that mode exists for experiments and must never reach the
    scheduler. To freeze, the agent should have just run
    ``contract_dry_run`` and confirmed the simulated output with the
    user.

    Args:
        spec (dict): The contract dict, possibly fresh from authoring
            or revised. Must validate.

    Returns:
        dict
    """
    try:
        c = _coerce_spec(spec)
    except Exception as e:
        return {"status": "error", "message": f"spec failed validation: {e}"}

    if c.enforcement != EnforcementMode.STRICT:
        return {
            "status": "error",
            "message": (
                "Refusing to freeze a contract with enforcement != STRICT. "
                "STRICT is the only production-acceptable mode."
            ),
        }

    frozen = contract_store.freeze(c)
    return {
        "status": "frozen",
        "id": frozen.id,
        "version": frozen.version,
        "hash": frozen.hash,
        "message": (
            f"Contract {frozen.id!r} v{frozen.version} frozen (hash "
            f"{frozen.hash[:12]}). Call contract_schedule next to wire "
            f"it into APScheduler."
        ),
    }


async def contract_schedule(contract_id: str, tool_context: ToolContext) -> dict:
    """Wire the latest frozen version of ``contract_id`` into
    APScheduler. Replaces any prior job for the same id, so revising
    + re-scheduling is one atomic switch.

    Args:
        contract_id (str): The contract id (as set in the spec).

    Returns:
        dict
    """
    try:
        c = contract_store.load_latest(contract_id)
    except ContractNotFound:
        return {"status": "error", "message": f"no contract with id {contract_id!r}"}

    return _schedule(c)


async def contract_unschedule(contract_id: str, tool_context: ToolContext) -> dict:
    """Remove ``contract_id`` from APScheduler. The frozen body stays
    on disk; only the active schedule is dropped.

    Args:
        contract_id (str): The contract id to unschedule.

    Returns:
        dict
    """
    return _unschedule(contract_id)


async def contract_revise(
    contract_id: str,
    new_spec: dict,
    tool_context: ToolContext,
) -> dict:
    """Freeze a new version of an existing contract, recording the
    previous version's hash as ``parent_hash`` so the audit chain is
    intact.

    Args:
        contract_id (str): The contract id to revise.
        new_spec (dict): The full updated contract dict. The ``id``
            must match ``contract_id``; ``version`` is ignored
            (assigned by the store).

    Returns:
        dict
    """
    try:
        prior = contract_store.load_latest(contract_id)
    except ContractNotFound:
        return {"status": "error", "message": f"no contract with id {contract_id!r}"}

    if not isinstance(new_spec, dict):
        return {"status": "error", "message": "new_spec must be a dict."}
    if new_spec.get("id") != contract_id:
        return {
            "status": "error",
            "message": "new_spec.id must match the contract_id you're revising.",
        }

    new_spec = {**new_spec, "parent_hash": prior.hash}
    return await contract_freeze(new_spec, tool_context)


async def contract_list(tool_context: ToolContext) -> dict:
    """List all known contracts (latest version of each), plus their
    APScheduler job state.

    Returns:
        dict
    """
    items: list[dict] = []
    for cid in contract_store.list_all():
        try:
            c = contract_store.load_latest(cid)
            items.append(
                {
                    "id": cid,
                    "version": c.version,
                    "hash": c.hash,
                    "description": c.description,
                    "trigger_type": c.trigger.type,
                }
            )
        except Exception as e:
            items.append({"id": cid, "error": str(e)})

    jobs = list_contract_jobs()
    job_by_id = {j["contract_id"]: j for j in jobs}
    for item in items:
        item["scheduled"] = item["id"] in job_by_id
        if item["scheduled"]:
            item["next_run"] = job_by_id[item["id"]]["next_run"]

    return {"status": "ok", "contracts": items}


async def contract_inspect(
    contract_id: str, tool_context: ToolContext, version: Optional[int] = None
) -> dict:
    """Return the full frozen body of a contract — useful for the
    agent to review before revising.

    Args:
        contract_id (str): Contract id.
        version (int | None): Specific version to load. Default =
            latest.

    Returns:
        dict
    """
    versions = contract_store.list_versions(contract_id)
    if not versions:
        return {"status": "error", "message": f"no contract with id {contract_id!r}"}

    target = versions[0]
    if version is not None:
        target = next((v for v in versions if v.version == version), None)
        if target is None:
            return {
                "status": "error",
                "message": f"no version {version} for contract {contract_id!r}",
            }

    try:
        c = contract_store.load(contract_id, target.hash)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "ok",
        "contract": c.model_dump(mode="json", by_alias=True),
    }


# ---------------------------------------------------------------------------
# Optional: draft a contract spec from a legacy scheduled job
# ---------------------------------------------------------------------------


async def contract_from_existing(job_id: str, tool_context: ToolContext) -> dict:
    """Sketch a draft contract spec that approximates a legacy
    ``schedule_recurring_task`` / ``schedule_one_off_task`` job.

    The draft is just a starting point — the agent must review, fill
    in proper ``inputs`` / ``reasoning`` / ``output`` schemas, then
    dry-run and freeze. Nothing is migrated automatically.

    Args:
        job_id (str): Legacy APScheduler job id (``cron_*`` or
            ``oneoff_*``).

    Returns:
        dict: ``{"status": "draft", "spec": {...}}`` or an error.
    """
    from app.scheduler_instance import scheduler

    try:
        job = scheduler.get_job(job_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    if job is None:
        return {"status": "error", "message": f"no job with id {job_id!r}"}

    kwargs = job.kwargs or {}
    task_prompt = kwargs.get("task_prompt", "")
    notify = kwargs.get("notify", {}) or {}

    # Derive a snake-case contract id from the job id.
    contract_id = job_id.replace("cron_", "c_").replace("oneoff_", "o_")

    spec: dict[str, Any] = {
        "id": contract_id,
        "description": f"Imported from legacy job {job_id}.",
        "author": tool_context.state.get("user_id", "unknown") if tool_context else "unknown",
        "trigger": _trigger_from_job(job),
        "inputs": [],
        "reasoning": [],
        "emit": [
            {
                "adapter": _adapter_from_notify(notify),
                "args": _emit_args_from_notify(notify, task_prompt),
            }
        ],
    }
    return {
        "status": "draft",
        "spec": spec,
        "message": (
            "Draft contract sketched from legacy job. Review + tighten "
            "(add inputs/reasoning if the task involves anything "
            "dynamic), then contract_dry_run → contract_freeze → "
            "contract_schedule. Delete the legacy job manually once "
            "you're confident."
        ),
    }


def _trigger_from_job(job) -> dict:
    """Best-effort decode of an APScheduler trigger back to a contract
    trigger dict. Cron jobs map cleanly; one-shots become on_demand."""
    try:
        from apscheduler.triggers.cron import CronTrigger as APSCronTrigger

        trig = job.trigger
        if isinstance(trig, APSCronTrigger):
            fields = " ".join(str(f) for f in trig.fields)
            tz = str(getattr(trig, "timezone", "UTC"))
            return {"type": "cron", "cron": fields, "timezone": tz}
    except Exception:
        pass
    return {"type": "on_demand"}


def _adapter_from_notify(notify: dict) -> str:
    """Pick a reasonable default emit adapter from the legacy notify
    dict. ``deliver_to_session`` like ``sl_*`` → slack_post,
    ``tg_*`` → telegram_dm."""
    deliver = (notify.get("deliver_to_session") or "").lower()
    if deliver.startswith("sl_"):
        return "slack_post"
    if deliver.startswith("tg_"):
        return "telegram_dm"
    return "slack_post"


def _emit_args_from_notify(notify: dict, task_prompt: str) -> dict:
    """Sketch emit args from the legacy notify config. Author must
    flesh these out — particularly the ``content`` should reference
    rendered reasoning output once the contract has a reasoning step."""
    deliver = notify.get("deliver_to_session") or ""
    return {
        "channel": deliver or "<set channel>",
        "content": (
            f"<rewrite this>: legacy task_prompt was:\n{task_prompt[:300]}"
        ),
    }
