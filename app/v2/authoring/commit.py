"""V2 scheduler — schedule_draft_commit authoring verb.

Phase 8 slice 4 per ``docs/PHASE_8_PLAN.md`` §3.4 + §5.4.

Lands a draft as a row in the v2 ``schedules`` table AND
appends a ``schedule_created`` EventLedger event in the
same transaction. Both writes succeed or both roll back —
the EventLedger is the audit source of truth and must never
diverge from the schedules table.

Pre-flight gates (same precedence as :func:`schedule_freeze`
post-slice-3 fix):

1. Missing draft → :meth:`ToolResponse.not_found`.
2. Incomplete draft → :meth:`ToolResponse.not_ready`.
3. to_spec naive-clock failure → ``to_spec_failed``.
4. **Trigger-type gate** (L87 / Q4):
   ``non_oneoff_trigger_blocked_until_real_mode`` BEFORE
   validate_schedule_spec so a cron-without-plan draft
   surfaces the phase-8 contract code, not an unrelated
   validation issue.
5. Validation failure → :meth:`ToolResponse.validation_failed`.
6. Missing handshake → ``dry_run_required``.
7. Expired handshake → ``dry_run_expired``.
8. Hash drift → ``body_hash_drift``.

Atomic write (reached only when every pre-flight gate
passes):

- :func:`app.v2.storage.transactions.transaction` opens an
  explicit ``BEGIN``.
- :func:`insert_schedule(conn, spec)` — surfaces
  :class:`sqlite3.IntegrityError` on a duplicate id; the
  context manager rolls back and the caller maps to
  ``validation_failed(duplicate_schedule_id)`` (round-1
  reviewer Q8 confirmed shape).
- :func:`append_event(conn, Event(kind=SCHEDULE_CREATED,
  ...))`. Event id from the injected
  ``event_id_factory``; ``ts`` from ``clock()`` normalised
  to UTC; payload is exactly
  ``{"hash": spec.hash, "template": <name|None>}``
  (round-1 reviewer Q6).
- Both writes succeed → the context manager issues
  ``COMMIT``.

Post-commit cleanup (round-1 reviewer L99 fix):

- Best-effort. After the DB TX commits successfully:
  attempt :meth:`DraftStore.delete`; any exception is
  caught, logged at WARNING on
  ``app.v2.authoring.commit`` naming the schedule id +
  the offending path, and DOES NOT raise.
- Same for :meth:`HandshakeStore.delete`. Independent
  try/except so a draft-delete failure does not prevent
  the handshake cleanup attempt.
- The response is still :meth:`ToolResponse.ok` even
  when one or both file deletes fail. The schedule is
  already on disk; the EventLedger entry is the source
  of truth.

References:
- ``docs/PHASE_8_PLAN.md`` §3.4 + §5.4
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.5, §5.6, §11.4
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.handshake import HandshakeStore
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import _validation_failed_single
from app.v2.enums import EventKind
from app.v2.models.event import Event
from app.v2.storage.events import append_event
from app.v2.storage.schedules import insert_schedule
from app.v2.storage.transactions import transaction
from app.v2.validation import validate_schedule_spec


logger = logging.getLogger("app.v2.authoring.commit")


_ONEOFF_TRIGGER_TYPE = "one_off"


async def schedule_draft_commit(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
    handshake_store: HandshakeStore,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Land the draft as a schedules row + append a
    ``schedule_created`` event in one transaction.

    Gates on the freeze shape (same handshake checks +
    same gate ordering as :func:`schedule_freeze`). On
    success:

    - Inserts the schedule.
    - Appends a ``schedule_created`` event with payload
      ``{"hash": spec.hash, "template": <name|None>}``.
    - Commits the TX.
    - Best-effort deletes the draft + handshake files
      (WARNING log on failure; response stays ``ok``).

    On any pre-flight gate failure OR a DB TX failure:

    - Rolls back the DB TX (the ``transaction`` context
      manager handles this on any raise inside the block).
    - Leaves the draft + handshake files on disk so the
      author can fix + retry.
    - Returns the :meth:`ToolResponse.validation_failed`
      shape produced by the failing gate (or
      ``validation_failed(duplicate_schedule_id)`` for the
      IntegrityError path).

    Success: :meth:`ToolResponse.ok` carrying
    ``schedule_id=spec.id`` and ``spec=spec.model_dump(
    mode="json")``.
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
    # handshake + DB I/O). Same ordering as freeze post-fix
    # so the non-OneOff contract is the LLM-visible code,
    # not a downstream validation issue. ----
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

    # ---- 5. Validation chokepoint ----
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
                "call schedule_dry_run before "
                "schedule_draft_commit"
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
                "schedule_draft_commit clock() returned a naive "
                "datetime; expiry check + event ts require "
                "tz-aware UTC"
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
                f"{draft_id!r} before committing"
            ),
        )

    # ---- 9. Atomic insert + schedule_created event ----
    template_name = (
        spec.template.name if spec.template is not None else None
    )
    event = Event(
        id=event_id_factory(),
        run_id=None,
        schedule_id=spec.id,
        ts=now_utc,
        kind=EventKind.SCHEDULE_CREATED,
        payload={"hash": spec.hash, "template": template_name},
        correlates=None,
    )

    try:
        with transaction(conn):
            insert_schedule(conn, spec)
            append_event(conn, event)
    except sqlite3.IntegrityError as exc:
        return _validation_failed_single(
            code="duplicate_schedule_id",
            path="id",
            message=(
                f"schedule id {spec.id!r} already exists in the "
                f"schedules table; storage refused the insert "
                f"({exc!s})"
            ),
        )

    # ---- 10. Best-effort post-commit cleanup ----
    # The DB TX is the audit source of truth. File-delete
    # failures DO NOT poison the response — the LLM gets
    # ``ok`` and the operator sees the WARNING in logs.
    try:
        store.delete(session_id, draft_id)
    except Exception:
        draft_path = None
        try:
            draft_path = store._resolve(session_id, draft_id)
        except Exception:
            pass
        logger.warning(
            "schedule_draft_commit: draft file cleanup failed "
            "for schedule_id=%s path=%s; commit succeeded "
            "(EventLedger is source of truth)",
            spec.id,
            draft_path,
            exc_info=True,
        )

    try:
        handshake_store.delete(session_id, draft_id)
    except Exception:
        handshake_path = None
        try:
            handshake_path = handshake_store._resolve(
                session_id, draft_id
            )
        except Exception:
            pass
        logger.warning(
            "schedule_draft_commit: handshake file cleanup "
            "failed for schedule_id=%s path=%s; commit "
            "succeeded (EventLedger is source of truth)",
            spec.id,
            handshake_path,
            exc_info=True,
        )

    return ToolResponse.ok(
        schedule_id=spec.id,
        spec=spec.model_dump(mode="json"),
    )


__all__ = ["schedule_draft_commit"]
