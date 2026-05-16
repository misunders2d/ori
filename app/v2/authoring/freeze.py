"""V2 scheduler — schedule_freeze authoring verb.

Phase 8 slice 3 per ``docs/PHASE_8_PLAN.md`` §3.3 + §5.3.

Re-validates a draft → spec, then runs the trigger-type
gate, then verifies the freshest
:class:`app.v2.authoring.handshake.HandshakeRecord` is
present / fresh / hash-matches before letting the commit
verb proceed.

Trigger-type gate (§12-step-aware): OneOff (step 9) AND
cron (step 11 — the phase-11 source-driven cutover
unlocks recurring source series via this same authoring
spine, extended additively) are allowed; every OTHER
non-OneOff trigger type (interval / event / conditional /
…) is still refused with code
``trigger_type_pending_step_unlock`` until its own
implementation step lands. The gate runs BEFORE the
handshake check so a still-gated draft with no handshake
surfaces the trigger-type code (not ``dry_run_required``).
Cron specs that pass the gate then run the FULL remaining
freeze validation (handshake / expiry / hash-drift) — the
un-gate removes ONLY the trigger-type bypass.

Failure shapes (in order of precedence):

- Missing draft → :meth:`ToolResponse.not_found`.
- Incomplete draft → :meth:`ToolResponse.not_ready`.
- to_spec naive-clock failure → ``to_spec_failed``.
- A still-gated trigger type (NOT OneOff, NOT cron) →
  ``trigger_type_pending_step_unlock``. Runs BEFORE
  validation so an unrelated validation issue cannot mask
  the LLM-visible reason.
- Validation failure → :meth:`ToolResponse.validation_failed`.
- Missing handshake → ``dry_run_required``.
- Expired handshake → ``dry_run_expired`` with elapsed
  seconds in the message.
- Hash drift → ``body_hash_drift``.

Success: :meth:`ToolResponse.ok` carrying
``spec=spec.model_dump(mode="json")``. The freeze itself
does NOT touch the v2 DB and does NOT delete the
handshake; both remain in place for the commit verb (slice
4).

References:
- ``docs/PHASE_8_PLAN.md`` §3.3 + §5.3
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.5, §5.6
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.handshake import HandshakeStore
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import _validation_failed_single
from app.v2.validation import validate_schedule_spec


_ONEOFF_TRIGGER_TYPE = "one_off"
#: Phase-11 §12 step-11: cron is unlocked for source-driven
#: recurring series through this same authoring spine
#: (extended additively in slice 7a). Other non-OneOff
#: trigger types stay gated until their own step.
_CRON_TRIGGER_TYPE = "cron"
_UNLOCKED_TRIGGER_TYPES = frozenset(
    {_ONEOFF_TRIGGER_TYPE, _CRON_TRIGGER_TYPE}
)


async def schedule_freeze(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
    handshake_store: HandshakeStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Verify the dry-run handshake is fresh + matches the
    draft, then return the canonical spec for the commit
    verb.

    OneOff (step 9) + cron (step 11) pass this gate; other
    non-OneOff trigger types are refused here (before the
    handshake check) so the commit verb never sees a
    still-gated trigger.

    Workflow:

    1. Load the draft via :meth:`DraftStore.read`. Missing →
       :meth:`ToolResponse.not_found`.
    2. Check :meth:`ScheduleSpecDraft.missing_required_fields`.
       Non-empty → :meth:`ToolResponse.not_ready`.
    3. Call :meth:`ScheduleSpecDraft.to_spec(clock=clock)`.
       Naive-clock failure → ``to_spec_failed``.
    4. **Trigger-type gate**: if ``spec.trigger.type`` is
       NOT in ``{"one_off", "cron"}`` →
       ``validation_failed(trigger_type_pending_step_unlock)``.
       Runs BEFORE validation AND the handshake check so an
       unrelated validation issue (e.g. cron without
       ``execution_plan_hash`` → reminder-only rule) cannot
       mask the trigger-type code the LLM needs.
    5. Call :func:`validate_schedule_spec(spec)` with NO
       kwargs. Issues → :meth:`ToolResponse.validation_failed`.
    6. Read the handshake via :meth:`HandshakeStore.read`.
       FileNotFoundError → ``validation_failed(dry_run_required)``.
    7. Expiry check: ``handshake.is_expired(now=clock())`` →
       ``validation_failed(dry_run_expired)`` with the
       elapsed seconds in the message.
    8. Hash drift check: ``handshake.body_hash != spec.hash``
       → ``validation_failed(body_hash_drift)``.
    9. Return :meth:`ToolResponse.ok` carrying
       ``spec=spec.model_dump(mode="json")``. The freeze
       does NOT mutate the DB and does NOT delete the
       handshake.
    """
    # ---- 1. Load draft ----
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return ToolResponse.not_found(
            message=f"draft {draft_id!r} not found"
        )

    # ---- 2. Completeness ----
    missing = draft.missing_required_fields()
    if missing:
        return ToolResponse.not_ready(missing_fields=missing)

    # ---- 3. to_spec ----
    try:
        spec = draft.to_spec(clock=clock)
    except ValueError as exc:
        return _validation_failed_single(
            code="to_spec_failed",
            path="<root>",
            message=str(exc),
        )

    # ---- 4. Trigger-type gate (BEFORE validation +
    # handshake checks). OneOff (step 9) + cron (step 11 —
    # phase-11 source-driven series, this spine extended
    # additively) pass; every OTHER non-OneOff type stays
    # gated pending its step. Gate runs immediately after
    # to_spec — running it after validate_schedule_spec
    # would let an unrelated validation failure (e.g. cron
    # without execution_plan_hash → reminder-only rule)
    # mask the trigger-type code the LLM needs to see.
    # C2: the un-gate removes ONLY this trigger-type
    # bypass — cron specs fall through to the FULL
    # remaining validation (step 5+) below. ----
    trigger_type = getattr(spec.trigger, "type", None)
    if trigger_type not in _UNLOCKED_TRIGGER_TYPES:
        return _validation_failed_single(
            code="trigger_type_pending_step_unlock",
            path="trigger.type",
            message=(
                f"trigger type {trigger_type!r} is not yet "
                f"unlocked; OneOff (§12 step 9) and cron "
                f"(§12 step 11) are authorable — the "
                f"remaining trigger types unlock with their "
                f"own implementation step"
            ),
        )

    # ---- 5. validation chokepoint ----
    result = validate_schedule_spec(spec)
    if not result.ok:
        return ToolResponse.validation_failed(
            issues=list(result.issues)
        )

    # ---- 6. Handshake presence ----
    try:
        handshake = handshake_store.read(session_id, draft_id)
    except FileNotFoundError:
        return _validation_failed_single(
            code="dry_run_required",
            path="<root>",
            message=(
                f"no dry-run handshake for draft {draft_id!r}; "
                "call schedule_dry_run before schedule_freeze"
            ),
        )

    # ---- 7. Expiry ----
    now = clock()
    if (
        now.tzinfo is None
        or now.tzinfo.utcoffset(now) is None
    ):
        return _validation_failed_single(
            code="to_spec_failed",
            path="<root>",
            message=(
                "schedule_freeze clock() returned a naive "
                "datetime; expiry check requires tz-aware UTC"
            ),
        )
    now_utc = now.astimezone(timezone.utc)
    if handshake.is_expired(now=now_utc):
        elapsed_seconds = (now_utc - handshake.expires_at).total_seconds()
        return _validation_failed_single(
            code="dry_run_expired",
            path="<root>",
            message=(
                f"dry-run handshake expired {elapsed_seconds:.1f}s "
                f"ago; re-run schedule_dry_run for draft "
                f"{draft_id!r}"
            ),
        )

    # ---- 8. Hash drift ----
    if handshake.body_hash != spec.hash:
        return _validation_failed_single(
            code="body_hash_drift",
            path="<root>",
            message=(
                "draft body changed after the dry-run handshake "
                "was recorded; re-run schedule_dry_run for draft "
                f"{draft_id!r} before freezing"
            ),
        )

    # ---- 9. Success: no DB mutation, no file deletion ----
    return ToolResponse.ok(spec=spec.model_dump(mode="json"))


__all__ = ["schedule_freeze"]
