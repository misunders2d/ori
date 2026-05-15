"""V2 scheduler — schedule_freeze authoring verb.

Phase 8 slice 3 per ``docs/PHASE_8_PLAN.md`` §3.3 + §5.3.

Re-validates a draft → spec, then runs the trigger-type
gate, then verifies the freshest
:class:`app.v2.authoring.handshake.HandshakeRecord` is
present / fresh / hash-matches before letting the commit
verb proceed.

Phase 8 is **OneOff-only**: cron / interval drafts are
refused outright with code
``non_oneoff_trigger_blocked_until_real_mode`` per round-1
reviewer L87 / Q4. Phase 10 / 12 lift the gate alongside
the ``real`` dry-run mode body. The gate runs BEFORE the
handshake check so a cron draft with no handshake at all
still surfaces the non-OneOff code (not
``dry_run_required``).

Failure shapes (in order of precedence):

- Missing draft → :meth:`ToolResponse.not_found`.
- Incomplete draft → :meth:`ToolResponse.not_ready`.
- to_spec naive-clock failure → ``to_spec_failed``.
- Non-OneOff trigger →
  ``non_oneoff_trigger_blocked_until_real_mode``. Runs
  BEFORE validation so an unrelated validation issue
  cannot mask the LLM-visible reason.
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

    Phase 8 is OneOff-only: cron / interval drafts are
    refused at this gate (before the handshake check) so the
    commit verb never sees a non-OneOff trigger in phase 8.

    Workflow:

    1. Load the draft via :meth:`DraftStore.read`. Missing →
       :meth:`ToolResponse.not_found`.
    2. Check :meth:`ScheduleSpecDraft.missing_required_fields`.
       Non-empty → :meth:`ToolResponse.not_ready`.
    3. Call :meth:`ScheduleSpecDraft.to_spec(clock=clock)`.
       Naive-clock failure → ``to_spec_failed``.
    4. **Trigger-type gate** (L87 / Q4): if
       ``spec.trigger.type != "one_off"`` →
       ``validation_failed(non_oneoff_trigger_blocked_until_real_mode)``.
       Runs BEFORE validation AND the handshake check so an
       unrelated validation issue (e.g. cron without
       ``execution_plan_hash`` → reminder-only rule) cannot
       mask the non-OneOff code the LLM needs.
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
    # handshake checks). Phase 8 is OneOff-only per round-1
    # reviewer L87 / Q4. Gate must run immediately after
    # to_spec — running it after validate_schedule_spec
    # would let an unrelated validation failure (e.g. cron
    # without execution_plan_hash → reminder-only rule)
    # mask the non-OneOff code the LLM needs to see. ----
    trigger_type = getattr(spec.trigger, "type", None)
    if trigger_type != _ONEOFF_TRIGGER_TYPE:
        return _validation_failed_single(
            code="non_oneoff_trigger_blocked_until_real_mode",
            path="trigger.type",
            message=(
                f"phase 8 is OneOff-only; trigger type "
                f"{trigger_type!r} requires the `real` dry-run "
                "mode (phase 10 / 12)"
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
