"""V2 scheduler — schedule_dry_run authoring verb.

Phase 8 slice 2 per ``docs/PHASE_8_PLAN.md`` §3.2 + §5.2.

Validates a draft and records a fresh
:class:`app.v2.authoring.handshake.HandshakeRecord` that the
phase-8 freeze + commit verbs gate on per design §5.6.

Three modes per :class:`DryRunMode`:

- ``validate_only`` — fully implemented. Pydantic +
  :func:`validate_schedule_spec(spec)` with NO
  ``execution_plans`` / ``registries`` kwargs (reminder-only
  carry-forward from phase 7). Writes a 60-second handshake
  on success.
- ``mocked_inputs`` — returns
  :meth:`ToolResponse.validation_failed` with code
  ``mode_not_implemented_in_phase_8``. Phase 10 / 12 ship
  the real implementation alongside ExecutionPlan body +
  source loaders.
- ``real`` — same shape as ``mocked_inputs``.

``as_of_datetime`` is accepted for all modes; in
``validate_only`` it is stored verbatim on the handshake
record but does NOT affect behaviour (no time-sensitive
loader logic in the reminder-only flow). Future modes
consume it.

References:
- ``docs/PHASE_8_PLAN.md`` §3.2 + §5.2
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.5, §5.6
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.handshake import (
    _HANDSHAKE_WINDOW_SECONDS,
    DryRunMode,
    HandshakeRecord,
    HandshakeStore,
)
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import _validation_failed_single
from app.v2.validation import validate_schedule_spec


async def schedule_dry_run(
    draft_id: str,
    mode: DryRunMode,
    *,
    session_id: str,
    store: DraftStore,
    handshake_store: HandshakeStore,
    clock: Callable[[], datetime],
    as_of_datetime: Optional[datetime] = None,
) -> ToolResponse:
    """Validate the draft and record a fresh handshake.

    Flow (``validate_only``):

    1. Load the draft via :meth:`DraftStore.read`. Missing
       → :meth:`ToolResponse.not_found`.
    2. Check :meth:`ScheduleSpecDraft.missing_required_fields`.
       Non-empty → :meth:`ToolResponse.not_ready`; no
       handshake written.
    3. Call :meth:`ScheduleSpecDraft.to_spec(clock=clock)`.
       Naive-clock failure surfaces as
       :meth:`ToolResponse.validation_failed` with code
       ``to_spec_failed``; no handshake written.
    4. Call :func:`validate_schedule_spec(spec)` with NO
       ``execution_plans`` / ``registries`` kwargs
       (reminder-only). Issues →
       :meth:`ToolResponse.validation_failed`; no handshake
       written.
    5. Build a :class:`HandshakeRecord`:

       - ``body_hash = spec.hash`` (canonical hash from
         :meth:`with_fresh_hash`; same value compile +
         freeze + commit recompute).
       - ``mode = DryRunMode.VALIDATE_ONLY``.
       - ``as_of_datetime`` = caller arg (stored verbatim,
         ignored in ``validate_only``).
       - ``recorded_at`` = ``clock()`` (already UTC-normalised
         by :meth:`to_spec`; we call ``clock()`` again here
         to capture the freeze-window start).
       - ``expires_at`` = ``recorded_at`` + 60 s.

    6. Persist via :meth:`HandshakeStore.write`.
    7. Return :meth:`ToolResponse.ok` carrying
       ``spec=spec.model_dump(mode="json")`` and a hint
       naming the freeze window.

    ``mocked_inputs`` / ``real`` short-circuit to
    :meth:`ToolResponse.validation_failed` with code
    ``mode_not_implemented_in_phase_8`` (no handshake
    written, no draft read attempted — the mode check runs
    first).
    """
    # Mode gate first: stubbed modes refuse before any I/O.
    if mode is DryRunMode.MOCKED_INPUTS or mode is DryRunMode.REAL:
        return _validation_failed_single(
            code="mode_not_implemented_in_phase_8",
            path="mode",
            message=(
                f"dry-run mode {mode.value!r} ships in phase "
                "10 / 12 alongside ExecutionPlan body + source "
                "loaders. Use validate_only for reminder-only "
                "schedules in phase 8."
            ),
        )

    # validate_only path.
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

    # Handshake recording. Second clock() call here so
    # ``recorded_at`` reflects the freeze-window start, not
    # the ``authored_at`` baked into the spec. ``to_spec``
    # already enforced tz-aware UTC on the first clock call;
    # a second call is permitted to return any tz-aware
    # value provided we normalise to UTC for the record
    # (HandshakeRecord rejects naive AND non-UTC).
    clock_value = clock()
    if (
        clock_value.tzinfo is None
        or clock_value.tzinfo.utcoffset(clock_value) is None
    ):
        return _validation_failed_single(
            code="to_spec_failed",
            path="<root>",
            message=(
                "schedule_dry_run clock() returned a naive "
                "datetime; recorded_at must be tz-aware UTC"
            ),
        )
    recorded_at = clock_value.astimezone(timezone.utc)

    record = HandshakeRecord(
        draft_id=draft_id,
        session_id=session_id,
        body_hash=spec.hash,
        mode=DryRunMode.VALIDATE_ONLY,
        as_of_datetime=as_of_datetime,
        recorded_at=recorded_at,
        expires_at=recorded_at
        + timedelta(seconds=_HANDSHAKE_WINDOW_SECONDS),
    )
    handshake_store.write(session_id, record)

    return ToolResponse.ok(
        spec=spec.model_dump(mode="json"),
        message=(
            f"dry-run handshake recorded; freeze within "
            f"{_HANDSHAKE_WINDOW_SECONDS}s"
        ),
    )


__all__ = ["schedule_dry_run"]
