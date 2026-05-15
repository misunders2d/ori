"""V2 scheduler — authoring compile + discard + list verbs.

Phase 7 slice 4 per ``docs/PHASE_7_PLAN.md`` §3.5 + §5.5.

Three tools that operate on already-authored drafts without
mutating the v2 DB:

- :func:`schedule_draft_compile` — validate the draft and
  return its canonical :class:`ScheduleSpec` body. Read-only.
- :func:`schedule_draft_discard` — delete a draft file.
  Idempotent.
- :func:`schedule_draft_list` — list draft ids for a session.

**No commit verb** in phase 7 (round-2 reviewer L95 / Q5):
landing a row in the schedules table before phase-8's
dry-run + freeze handshake would (a) contradict the design
freeze gate and (b) bypass the ``schedule_created``
EventLedger entry the audit trail requires. Phase 8 ships
the atomic ``freeze + commit + schedule_created`` triplet.

References:
- ``docs/PHASE_7_PLAN.md`` §3.5 + §5.5
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.5 (validation
  chokepoint), §5.6 (dry-run handshake, deferred).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import _validation_failed_single
from app.v2.validation import validate_schedule_spec


async def schedule_draft_compile(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Validate the draft and return the canonical
    :class:`ScheduleSpec` body.

    Does NOT freeze, does NOT write to the v2 DB, does NOT
    delete the draft file. The phase-8 freeze + commit
    handshake takes over from here.

    Flow:

    1. Load the draft via :meth:`DraftStore.read`. Missing
       → :meth:`ToolResponse.not_found`.
    2. Check :meth:`ScheduleSpecDraft.missing_required_fields`.
       Non-empty → :meth:`ToolResponse.not_ready` with the
       list.
    3. Call :meth:`ScheduleSpecDraft.to_spec`. A naive-clock
       error here surfaces as
       :meth:`ToolResponse.validation_failed` so the LLM
       sees a clean shape (the toolset wires a UTC clock by
       construction; this branch covers test-rig misuse).
    4. Call :func:`validate_schedule_spec(spec)` with NO
       ``execution_plans`` / ``registries`` kwargs (round-2
       reviewer Q6 — reminder-only flow phase 7 ships).
       Issues → :meth:`ToolResponse.validation_failed`.
    5. On success: :meth:`ToolResponse.ok` with
       ``spec=spec.model_dump()``.
    """
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return ToolResponse.not_found(
            message=f"draft {draft_id!r} not found"
        )

    missing = draft.missing_required_fields()
    if missing:
        return ToolResponse.not_ready(missing_fields=missing)

    try:
        spec = draft.to_spec(clock=clock)
    except ValueError as exc:
        return _validation_failed_single(
            code="to_spec_failed",
            path="<root>",
            message=str(exc),
        )

    result = validate_schedule_spec(spec)
    if not result.ok:
        return ToolResponse.validation_failed(
            issues=list(result.issues)
        )

    return ToolResponse.ok(spec=spec.model_dump(mode="json"))


async def schedule_draft_discard(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
) -> ToolResponse:
    """Delete the draft file.

    Idempotent — repeat call returns
    :meth:`ToolResponse.ok` with an ``already absent``
    hint in ``message``. The draft store's
    :meth:`DraftStore.delete` already tolerates missing
    files; this tool surfaces the no-op shape so the LLM
    can distinguish a fresh delete from a redundant one.
    """
    # ``DraftStore._resolve`` runs the slug fence + symlink
    # guard before we touch the filesystem; the call below
    # raises ValueError for invalid slugs which surfaces as
    # the caller's error (intentional — the tool layer
    # treats malformed ids as a programming bug, not user
    # input).
    existed_before = (
        store._resolve(session_id, draft_id).exists()
    )
    store.delete(session_id, draft_id)

    if existed_before:
        return ToolResponse.ok(draft_id=draft_id)
    return ToolResponse.ok(
        draft_id=draft_id,
        message=(
            f"draft {draft_id!r} already absent; delete was a "
            "no-op"
        ),
    )


async def schedule_draft_list(
    session_id: str,
    *,
    store: DraftStore,
) -> ToolResponse:
    """Return the draft ids for ``session_id`` in
    deterministic lexicographic order. Empty session →
    :meth:`ToolResponse.ok` with an empty list in
    ``message`` (the response shape carries the list via a
    structured payload — see below)."""
    ids = store.list_ids(session_id)
    # ``ToolResponse`` doesn't have a dedicated id-list
    # payload field; the spec dict is the closest match and
    # round-trips through the model. Use ``spec={"draft_ids":
    # [...]}`` for symmetry with compile's spec payload.
    return ToolResponse.ok(spec={"draft_ids": ids})


__all__ = [
    "schedule_draft_compile",
    "schedule_draft_discard",
    "schedule_draft_list",
]
