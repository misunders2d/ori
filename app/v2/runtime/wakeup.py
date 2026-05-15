"""Wakeup callback for the v2 runtime.

Phase 4 slices 5a + 5b per ``docs/PHASE_4_PLAN.md`` §6.2 / §6.5.

The wakeup function is the callable that APScheduler will
eventually register against each ScheduleSpec. Phase 4 ships
only the function — no real scheduler construction, no
registration glue. Tests invoke it directly with a synthetic
``now``.

Wired trigger variants:

- ``OneOffTrigger`` (slice 5a): fires when ``at_iso_datetime
  <= now``. Non-idempotent across calls — the registration
  layer (later phase) is responsible for unregistering after
  the fire.
- ``CronTrigger`` (slice 5b): fires when ``now`` aligns
  exactly with one of the cron expression's fire instants in
  the trigger's timezone. Computed via APScheduler's
  forward-only ``CronTrigger.from_crontab(...)
  .get_next_fire_time(None, now_local)`` — no inverse cron
  math, no "most recent past fire" semantics. Late wakeups
  are NOT backfilled; that lives behind the design's
  ``backfill_policy`` and lands in later phases.

Unwired trigger variants (raise ``NotImplementedError`` with
explicit per-type message): ``IntervalTrigger`` /
``EventTrigger`` / ``ConditionalTrigger``.

Per design §4.0.4 row 4 (ledger transactionality), every Run
row INSERT lands in the same transaction as the matching
``run_created`` event row. Wakeup uses
``app.v2.storage.transactions.transaction`` as the context
manager + raw SQL INSERTs because the helper for paired UPDATE
+ event (``update_run_status_and_append_event``) is shaped
for transitions, not inserts.

Both the Run id and the Event id are injected via the
``run_id_factory`` / ``event_id_factory`` callables. The
module does NOT import ``uuid``; smoke tests pin this.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0, §4.0.4, §4.2
- ``docs/PHASE_4_PLAN.md`` §6.2, §6.5
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import (
    CronTrigger as APSchedulerCronTrigger,
)

from app.v2.enums import EventKind, FireReason, RunStatus, ScheduleStatus
from app.v2.models.triggers import (
    ConditionalTrigger,
    CronTrigger,
    EventTrigger,
    IntervalTrigger,
    OneOffTrigger,
)
from app.v2.runtime.cron_guard import reject_numeric_dow
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.schedules import get_schedule
from app.v2.storage.serialization import NaiveDatetimeError, encode_json
from app.v2.storage.transactions import transaction


def wakeup(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    now: datetime,
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
) -> list[str]:
    """Read the ScheduleSpec for ``schedule_id``, compute any
    due fire times, and INSERT one or more pending Run rows +
    matching ``run_created`` events in the same transaction.

    Returns the list of inserted run ids (empty when nothing
    fires this call).

    No-ops when:

    - the schedule does not exist;
    - the schedule is paused or archived;
    - the trigger is not yet due (OneOff: ``at_iso_datetime``
      is still in the future).

    Trigger dispatch:

    - ``OneOffTrigger``: insert one Run when
      ``trigger.at_iso_datetime <= now``. Cleanup
      (unregistering the schedule) is the registration
      layer's job; phase-4 wakeup itself never dedups —
      calling it twice on a still-active past OneOff would
      create two Run rows.
    - ``CronTrigger``: fires when ``now`` aligns exactly with
      a cron fire instant via APScheduler's forward
      ``get_next_fire_time(None, now_local)``. Raises
      ``ValueError`` for numeric day-of-week, unknown
      timezone, or a cron expression APScheduler refuses.
    - ``IntervalTrigger`` / ``EventTrigger`` /
      ``ConditionalTrigger``: raise ``NotImplementedError``
      with explicit per-type message — these stay unwired
      through phase 4.

    Args:
        conn: caller-owned migrated SQLite connection. MUST NOT
            already be inside a transaction (wakeup opens its
            own).
        schedule_id: PK of the ScheduleSpec to fire.
        now: tz-aware boot/wakeup timestamp. Naive datetimes
            raise ``NaiveDatetimeError`` before any SQL runs.
        run_id_factory: callable producing a fresh id for each
            new pending Run row. Invoked once per row to be
            inserted (zero times if no row fires).
        event_id_factory: callable producing a fresh id for
            each new ``run_created`` event row. Invoked once
            per row to be inserted.

    Raises:
        NaiveDatetimeError: ``now`` was naive.
        ConnectionNotReady: bad connection state.
        ValueError: cron expression is invalid, the cron's
            day-of-week field contains a digit (numeric DOW
            is rejected per the slice-5b name-only constraint),
            or the trigger's timezone string cannot be
            resolved by ``ZoneInfo``.
        NotImplementedError: trigger type is ``interval``,
            ``event``, or ``conditional`` (these stay unwired
            through phase 4).

    Notes on schedule-status TOCTOU: both the
    ``get_schedule`` read and the subsequent inserts run
    inside a single ``transaction(conn)`` block. SQLite WAL
    serialises writers, so a concurrent pause / archive
    attempt either lands before our BEGIN (we see the new
    status) or after our COMMIT (our inserts have already
    landed). There is no in-flight window where we could read
    "active", get paused, and still insert.
    """
    # Validate ``now`` BEFORE ``assert_connection_ready``
    # (which runs PRAGMA / SELECT against the DB), so a
    # bad-arg caller never touches the database. Same
    # ordering rule used in scan_stale_runs after the round-4
    # reviewer fix.
    if now.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in now: {now!r} — attach tzinfo "
            "(typically datetime.timezone.utc) before passing."
        )
    assert_connection_ready(conn)

    inserted: list[str] = []
    with transaction(conn):
        spec = get_schedule(conn, schedule_id)
        if spec is None:
            return []
        if spec.status != ScheduleStatus.ACTIVE:
            return []

        trigger = spec.trigger
        if isinstance(trigger, OneOffTrigger):
            run_id = _insert_one_off_run(
                conn,
                spec_id=spec.id,
                trigger=trigger,
                now=now,
                run_id_factory=run_id_factory,
                event_id_factory=event_id_factory,
            )
            if run_id is not None:
                inserted.append(run_id)
            return inserted

        if isinstance(trigger, CronTrigger):
            run_id = _insert_cron_run(
                conn,
                spec_id=spec.id,
                trigger=trigger,
                now=now,
                run_id_factory=run_id_factory,
                event_id_factory=event_id_factory,
            )
            if run_id is not None:
                inserted.append(run_id)
            return inserted

        if isinstance(
            trigger, (IntervalTrigger, EventTrigger, ConditionalTrigger)
        ):
            raise NotImplementedError(
                f"Wakeup for trigger type {trigger.type!r} is "
                "not implemented in phase 4. The variant is "
                "shape-only until its use case ships in a "
                "later phase."
            )

        # Discriminated union exhaustion — every variant above
        # is named; this should be unreachable while the union
        # holds five members. Loud failure if a new variant
        # lands without a wakeup branch.
        raise NotImplementedError(
            f"Unknown trigger type {type(trigger).__name__!r} "
            "— wakeup dispatch is missing a branch. Add the "
            "variant in app/v2/runtime/wakeup.py."
        )


def _insert_one_off_run(
    conn: sqlite3.Connection,
    *,
    spec_id: str,
    trigger: OneOffTrigger,
    now: datetime,
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
) -> str | None:
    """Insert a pending Run + run_created event when the
    OneOff trigger is due. Returns the new run id, or None
    when ``at_iso_datetime`` is still in the future.

    Called INSIDE the wakeup's transaction; opens no nested
    transaction. The ``run_created`` event's ``ts`` is the
    wakeup time (``now``), not the trigger's
    ``at_iso_datetime`` — the event records when the run was
    created, not when it was supposed to fire. ``due_at`` on
    the Run row carries the firing time.
    """
    fire_at = trigger.at_iso_datetime
    if fire_at.tzinfo is None:
        # Pydantic v2 normally parses ISO 8601 to a tz-aware
        # datetime; defensive guard so a hand-constructed
        # trigger with a naive datetime fails loudly here
        # rather than silently storing an ambiguous string.
        raise NaiveDatetimeError(
            f"OneOffTrigger.at_iso_datetime is naive: "
            f"{fire_at!r}. The wakeup layer requires tz-aware "
            "trigger datetimes — re-author the spec."
        )
    if fire_at > now:
        return None

    run_id = run_id_factory()
    event_id = event_id_factory()
    fire_at_iso = fire_at.astimezone(timezone.utc).isoformat()
    now_iso = now.astimezone(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, "
        " attempt, root_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            spec_id,
            FireReason.SCHEDULED.value,
            fire_at_iso,
            RunStatus.PENDING.value,
            1,
            # First-attempt self-reference (Run model
            # invariant: attempt==1 → root_run_id == id).
            run_id,
        ),
    )
    conn.execute(
        "INSERT INTO events "
        "(id, run_id, schedule_id, ts, kind, "
        " payload_json, correlates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            run_id,
            spec_id,
            now_iso,
            EventKind.RUN_CREATED.value,
            encode_json(
                {
                    "fire_at": fire_at_iso,
                    "trigger_type": trigger.type,
                }
            ),
            None,
        ),
    )
    return run_id


def _insert_cron_run(
    conn: sqlite3.Connection,
    *,
    spec_id: str,
    trigger: CronTrigger,
    now: datetime,
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
) -> str | None:
    """Insert a pending Run + run_created event when ``now``
    exactly aligns with one of the cron expression's fire
    instants in the trigger's timezone. Returns the new run
    id, or ``None`` when ``now`` is between fires.

    Called INSIDE the wakeup's transaction; opens no nested
    transaction.

    Forward-only fire detection: ``CronTrigger.from_crontab(
    cron, timezone=tz).get_next_fire_time(None, now_local)``.
    APScheduler 3.11.x returns ``now_local`` itself when it
    aligns with a fire instant (probed for this version), and
    returns the next future instant otherwise. We compare the
    returned instant against ``now`` on the UTC time line so
    timezone choice does not influence equality. No inverse
    cron math, no "most recent past fire" semantics — that
    sort of late-fire bookkeeping belongs in the design's
    ``backfill_policy``, which phase 4 does NOT wire up.

    Raises ``ValueError`` on invalid timezone, numeric DOW, or
    a cron expression APScheduler refuses to parse.
    """
    reject_numeric_dow(trigger.cron)

    try:
        tz = ZoneInfo(trigger.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"unknown timezone {trigger.timezone!r} in cron "
            f"trigger: {exc}."
        ) from exc

    try:
        aps_trigger = APSchedulerCronTrigger.from_crontab(
            trigger.cron, timezone=tz
        )
    except ValueError as exc:
        # APScheduler raises ValueError with informative
        # messages already (probed: "Wrong number of fields",
        # "Invalid expression ..."). Re-raise with context so
        # the wakeup caller knows which schedule's cron broke.
        raise ValueError(
            f"invalid cron expression {trigger.cron!r}: {exc}"
        ) from exc

    now_local = now.astimezone(tz)
    next_fire = aps_trigger.get_next_fire_time(None, now_local)
    if next_fire is None:
        # APScheduler exhausted its forward search — e.g. a
        # cron with explicit year past + month/day combinations
        # that can never happen. Treat as no-op.
        return None
    if (
        next_fire.astimezone(timezone.utc)
        != now_local.astimezone(timezone.utc)
    ):
        # ``now`` is between fires. APScheduler returned the
        # NEXT future fire; we do not insert.
        return None

    run_id = run_id_factory()
    event_id = event_id_factory()
    fire_at_iso = next_fire.astimezone(timezone.utc).isoformat()
    now_iso = now.astimezone(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, "
        " attempt, root_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            spec_id,
            FireReason.SCHEDULED.value,
            fire_at_iso,
            RunStatus.PENDING.value,
            1,
            run_id,
        ),
    )
    conn.execute(
        "INSERT INTO events "
        "(id, run_id, schedule_id, ts, kind, "
        " payload_json, correlates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            run_id,
            spec_id,
            now_iso,
            EventKind.RUN_CREATED.value,
            encode_json(
                {
                    "fire_at": fire_at_iso,
                    "trigger_type": trigger.type,
                    "cron": trigger.cron,
                    "cron_timezone": trigger.timezone,
                }
            ),
            None,
        ),
    )
    return run_id


__all__ = ["wakeup"]
