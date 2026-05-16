"""V2 scheduler — schedule_draft_commit authoring verb.

Phase 8 slice 4 per ``docs/PHASE_8_PLAN.md`` §3.4 + §5.4.

Lands a draft as a row in the v2 ``schedules`` table —
AND, for a source-driven draft (``execution_plan_hash``
set, phase-11 7a), the carried ``ExecutionPlan`` body as
a row in ``execution_plans`` — AND appends a
``schedule_created`` EventLedger event, ALL in the SAME
transaction. Every write succeeds or every write rolls
back — the EventLedger is the audit source of truth and
must never diverge from the schedules / execution_plans
tables; a schedule is never persisted pointing at an
absent plan.

Pre-flight gates (same precedence as :func:`schedule_freeze`):

1. Missing draft → :meth:`ToolResponse.not_found`.
2. Incomplete draft → :meth:`ToolResponse.not_ready`.
3. to_spec naive-clock failure → ``to_spec_failed``.
4. **Trigger-type gate** (§12-step-aware): a trigger type
   NOT in ``{one_off, cron}`` →
   ``trigger_type_pending_step_unlock`` BEFORE
   validate_schedule_spec (OneOff = step 9, cron = step 11;
   others gated pending their step). A cron-without-plan
   draft therefore now surfaces the reminder-rule
   validation (``missing_execution_plan_for_complex_trigger``),
   NOT a trigger-type code — the cron un-gate is
   trigger-type-only.
5. Validation failure → :meth:`ToolResponse.validation_failed`.
6. Missing handshake → ``dry_run_required``.
7. Expired handshake → ``dry_run_expired``.
8. Hash drift → ``body_hash_drift``.
8b. ExecutionPlan-body consistency (source-driven only):
   missing / not-frozen / hash-mismatch / reasoning-bearing
   / present-without-hash → an explicit clean
   ``validation_failed`` code BEFORE the transaction
   (never an unhandled raise).

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
  (round-1 reviewer Q6). An ``IntegrityError`` here
  (event-id collision under the ``events.id`` primary key)
  rolls the TX back AND surfaces as
  ``validation_failed(duplicate_event_id)`` — the slice-4
  reviewer fix discriminates the two storage helpers via
  an inner-try sentinel so the LLM never sees a
  ``duplicate_schedule_id`` code for an event-side cause.
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
from app.v2.storage.execution_plans import (
    get_execution_plan,
    insert_execution_plan,
)
from app.v2.storage.schedules import insert_schedule
from app.v2.storage.transactions import transaction
from app.v2.validation import validate_schedule_spec


logger = logging.getLogger("app.v2.authoring.commit")


_ONEOFF_TRIGGER_TYPE = "one_off"
#: Twin of freeze._UNLOCKED_TRIGGER_TYPES — the commit verb
#: carries the SAME §12-step-aware gate (OneOff step 9 +
#: cron step 11). Kept in lock-step so a cron spec that
#: passes freeze is never re-blocked at commit.
_CRON_TRIGGER_TYPE = "cron"
_UNLOCKED_TRIGGER_TYPES = frozenset(
    {_ONEOFF_TRIGGER_TYPE, _CRON_TRIGGER_TYPE}
)


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
    # handshake + DB I/O). Same §12-step-aware gate as
    # freeze (OneOff step 9 + cron step 11) so the
    # trigger-type contract is the LLM-visible code, not a
    # downstream validation issue. C2: cron specs fall
    # through to the FULL remaining validation below. ----
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

    # ---- 8b. ExecutionPlan body consistency (phase-11 7a,
    # C3/C5). A source-driven spec (execution_plan_hash
    # set) MUST carry the matching frozen ExecutionPlan
    # body on the draft; a reminder spec MUST NOT. Every
    # mismatch is a CLEAN, EXPLICIT validation failure
    # surfaced BEFORE the DB transaction — never an
    # unhandled raise that could half-commit. C5: the
    # step-12 boundary is enforced HERE too — a
    # reasoning-bearing plan is refused at authoring (the
    # cron un-gate is trigger-type-only). ----
    plan = draft.execution_plan
    if spec.execution_plan_hash is not None:
        if plan is None:
            return _validation_failed_single(
                code="execution_plan_body_missing",
                path="execution_plan",
                message=(
                    "spec.execution_plan_hash is set but the "
                    "draft carries no execution_plan body; a "
                    "source-driven authoring path must attach "
                    "the compiled ExecutionPlan"
                ),
            )
        if not plan.hash:
            return _validation_failed_single(
                code="execution_plan_not_frozen",
                path="execution_plan.hash",
                message=(
                    "the attached ExecutionPlan is not frozen "
                    "(empty hash); call with_fresh_hash() "
                    "before committing"
                ),
            )
        if plan.hash != spec.execution_plan_hash:
            return _validation_failed_single(
                code="execution_plan_hash_mismatch",
                path="execution_plan_hash",
                message=(
                    f"draft.execution_plan.hash {plan.hash!r} "
                    f"!= spec.execution_plan_hash "
                    f"{spec.execution_plan_hash!r}; the body "
                    f"and the reference disagree"
                ),
            )
        if plan.reasoning:
            return _validation_failed_single(
                code="execution_plan_reasoning_unsupported_pending_step_12",
                path="execution_plan.reasoning",
                message=(
                    f"the attached ExecutionPlan carries "
                    f"{len(plan.reasoning)} reasoning step(s); "
                    f"the reasoning executor lands in §12 "
                    f"step 12 — phase-11 source-driven plans "
                    f"are inputs+emit, zero reasoning"
                ),
            )
    elif plan is not None:
        return _validation_failed_single(
            code="execution_plan_without_hash",
            path="execution_plan",
            message=(
                "draft carries an execution_plan body but "
                "spec.execution_plan_hash is None; "
                "OneOff/reminder specs must not attach a plan"
            ),
        )

    # ---- 9. Atomic insert(s) + schedule_created event ----
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

    # Inner-try sentinel discriminates which storage helper
    # raised: ``insert_schedule`` IntegrityError surfaces as
    # ``duplicate_schedule_id``; an ``append_event``
    # IntegrityError (e.g. event-id collision under the
    # events.id PRIMARY KEY) surfaces as
    # ``duplicate_event_id`` so the LLM sees the actual
    # cause. Reviewer slice-4 verdict: a single outer
    # ``except sqlite3.IntegrityError`` mis-attributes the
    # event-side collision as a schedule-id duplicate.
    insert_raised_integrity = False
    plan_insert_raised_integrity = False
    try:
        with transaction(conn):
            # Source-driven ONLY: insert the ExecutionPlan
            # FIRST so the schedule never points at an
            # absent plan (C3 — both-or-neither, ONE
            # transaction). The OneOff/reminder path
            # (plan is None) NEVER enters this branch —
            # exactly the single insert_schedule as before
            # (C3/C6b). Plans are content-addressed and may
            # be shared by multiple schedules
            # (execution_plans.py contract): if the identical
            # frozen body is already present, REUSE it (skip
            # the insert) rather than fail — a benign
            # idempotent case, not a duplicate error.
            if plan is not None:
                if get_execution_plan(conn, plan.hash) is None:
                    try:
                        insert_execution_plan(conn, plan)
                    except sqlite3.IntegrityError:
                        # TOCTOU: another writer inserted the
                        # same hash between the check and the
                        # insert. Roll the WHOLE TX back (the
                        # schedule has NOT been inserted yet —
                        # no orphan either way).
                        plan_insert_raised_integrity = True
                        raise
            try:
                insert_schedule(conn, spec)
            except sqlite3.IntegrityError:
                insert_raised_integrity = True
                raise
            append_event(conn, event)
    except sqlite3.IntegrityError as exc:
        if plan_insert_raised_integrity:
            return _validation_failed_single(
                code="duplicate_execution_plan",
                path="execution_plan.hash",
                message=(
                    f"execution_plan hash "
                    f"{spec.execution_plan_hash!r} was "
                    f"inserted concurrently; the whole commit "
                    f"rolled back (no orphan schedule). Retry "
                    f"({exc!s})"
                ),
            )
        if insert_raised_integrity:
            return _validation_failed_single(
                code="duplicate_schedule_id",
                path="id",
                message=(
                    f"schedule id {spec.id!r} already exists in "
                    f"the schedules table; storage refused the "
                    f"insert ({exc!s})"
                ),
            )
        # Event-side collision: insert succeeded, append
        # blew up. Surface as ``duplicate_event_id`` so the
        # caller can retry with a fresh event id factory.
        return _validation_failed_single(
            code="duplicate_event_id",
            path="event.id",
            message=(
                f"schedule_created event id collision while "
                f"committing schedule {spec.id!r}; the schedules "
                f"insert rolled back. Retry with a fresh "
                f"event_id_factory output ({exc!s})"
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
