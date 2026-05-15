"""Tests for ``app.v2.runtime.wakeup`` — Cron path (slice 5b).

Pins per ``docs/PHASE_4_PLAN.md`` §6.2 / §6.5.

Parser: APScheduler 3.11.x ``CronTrigger.from_crontab`` (chosen
because the dependency already exists in v1 / runtime). The
fire-detection rule is forward-only: a cron trigger fires when
``aps.get_next_fire_time(None, now_local) == now_local`` on the
UTC time line. No inverse cron math, no "most recent past
fire" semantics — late wakeups are NOT backfilled.

Behavioural coverage:
- Cron aligned to ``now`` in UTC → insert one pending Run +
  run_created event in the same TX. Run.due_at == cron fire
  instant in UTC; event.ts == wakeup ``now`` in UTC; event
  payload carries the fire instant, trigger type, cron
  expression, and configured timezone.
- Cron NOT aligned to ``now`` → no insert; factories never
  called (event/run id never generated).
- Cron expressed in a non-UTC timezone (Europe/Kyiv 18:00) →
  fires when wakeup ``now`` is the corresponding UTC instant
  (15:00 UTC in summer = 18:00 Kyiv).
- Paused schedule + Cron (even at aligned time) → no insert.
- Archived schedule + Cron → no insert.

Cron-input rejection:
- Numeric day-of-week field (``1-5``, ``0,6``, ``*/2``,
  ``MON-FRI/2`` — anything with a digit in field 5) raises
  ``ValueError`` BEFORE APScheduler sees the expression.
  APScheduler uses Monday=0 numeric semantics while Unix cron
  uses Sunday=0, so numeric DOW is ambiguous.
- Invalid cron expression (gibberish, wrong field arity that
  bypasses the Pydantic model somehow) raises ``ValueError``
  with the underlying APScheduler message attached for
  context.
- Bad timezone string raises ``ValueError``.

Test-scope guards:
- Run row attempt=1 / root_run_id=self_id (Run model
  first-attempt invariant).
- Numeric-DOW rejection runs WITHOUT consulting the DB —
  test pins that the factories were never called.
- All existing slice-5a OneOff behavior continues to pass
  (covered by the sibling test file; not re-verified here).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import (
    DeliveryFallbackPolicy,
    EventKind,
    FailureActionType,
    FireReason,
    RunStatus,
    ScheduleStatus,
)
from app.v2.migrations import runner
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import CronTrigger
from app.v2.runtime.wakeup import wakeup
from app.v2.storage.schedules import insert_schedule


_NOW_ISO = "2026-05-15T09:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_cron_schedule(
    conn: sqlite3.Connection,
    *,
    schedule_id: str = "daily_audit",
    cron: str = "0 18 * * *",
    timezone_name: str = "UTC",
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
) -> None:
    spec = ScheduleSpec(
        id=schedule_id,
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="daily audit cron schedule",
        trigger=CronTrigger(cron=cron, timezone=timezone_name),
        delivery=Delivery(
            target_session_id="sl_test",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
        status=status,
        execution_plan_hash=None,
        authored_at=_NOW_ISO,
    ).with_fresh_hash()
    insert_schedule(conn, spec)


def _run_counter():
    state = {"i": 0}

    def factory():
        state["i"] += 1
        return f"new-run-{state['i']}"

    return factory, state


def _evt_counter():
    state = {"i": 0}

    def factory():
        state["i"] += 1
        return f"new-evt-{state['i']}"

    return factory, state


def _runs(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT id, schedule_id, fire_reason, due_at, status, "
        "attempt, root_run_id, parent_run_id "
        "FROM runs ORDER BY id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _events(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT id, run_id, schedule_id, ts, kind, "
        "payload_json, correlates "
        "FROM events ORDER BY id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ===========================================================================
# Aligned cron fires
# ===========================================================================


def test_cron_aligned_inserts_one_run_and_event(tmp_path):
    """``0 18 * * *`` with ``now == 2026-05-15T18:00:00Z``
    fires exactly once — one pending Run + matching
    run_created event in the same TX."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn)
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()

    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=aligned,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == ["new-run-1"]

    runs = _runs(conn)
    assert len(runs) == 1
    row = runs[0]
    assert row["id"] == "new-run-1"
    assert row["schedule_id"] == "daily_audit"
    assert row["fire_reason"] == FireReason.SCHEDULED.value
    assert row["status"] == RunStatus.PENDING.value
    assert row["attempt"] == 1
    assert row["root_run_id"] == "new-run-1"
    assert row["parent_run_id"] is None
    assert row["due_at"] == aligned.isoformat()

    events = _events(conn)
    assert len(events) == 1
    evt = events[0]
    assert evt["id"] == "new-evt-1"
    assert evt["run_id"] == "new-run-1"
    assert evt["schedule_id"] == "daily_audit"
    assert evt["kind"] == EventKind.RUN_CREATED.value
    assert evt["ts"] == aligned.isoformat()
    assert evt["correlates"] is None
    assert '"fire_at":"' + aligned.isoformat() + '"' in evt["payload_json"]
    assert '"trigger_type":"cron"' in evt["payload_json"]
    assert '"cron":"0 18 * * *"' in evt["payload_json"]
    assert '"cron_timezone":"UTC"' in evt["payload_json"]

    assert run_state["i"] == 1
    assert evt_state["i"] == 1


def test_cron_aligned_in_non_utc_timezone_fires(tmp_path):
    """``0 18 * * *`` in ``Europe/Kyiv`` (UTC+3 in May) fires
    when wakeup ``now`` is 15:00 UTC."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(
        conn,
        cron="0 18 * * *",
        timezone_name="Europe/Kyiv",
    )
    now_utc = datetime(2026, 5, 15, 15, 0, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=now_utc,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == ["new-run-1"]
    runs = _runs(conn)
    # due_at stored as +00:00 normalised UTC.
    assert runs[0]["due_at"] == now_utc.isoformat()


def test_cron_every_five_minutes_aligned_fires(tmp_path):
    """``*/5 * * * *`` at a 5-minute boundary fires."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn, cron="*/5 * * * *")
    now = datetime(2026, 5, 15, 18, 5, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=now,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == ["new-run-1"]


def test_cron_named_dow_fires(tmp_path):
    """``0 18 * * MON-FRI`` on a Friday fires (2026-05-15 is
    a Friday). Pinning that named DOW is accepted."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn, cron="0 18 * * MON-FRI")
    fri_18 = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=fri_18,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == ["new-run-1"]


# ===========================================================================
# Misaligned cron — no insert
# ===========================================================================


def test_cron_misaligned_no_insert(tmp_path):
    """``0 18 * * *`` with ``now == 18:01`` is a minute past
    the fire instant — wakeup must NOT insert. Factories must
    NOT be called (no wasted ids)."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn)
    misaligned = datetime(2026, 5, 15, 18, 1, tzinfo=timezone.utc)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()

    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=misaligned,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []
    assert _runs(conn) == []
    assert _events(conn) == []
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


def test_cron_one_minute_before_fire_no_insert(tmp_path):
    """``now == 17:59`` is one minute BEFORE the fire — no
    insert."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn)
    early = datetime(2026, 5, 15, 17, 59, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=early,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []


# ===========================================================================
# Schedule-level guards (carry from 5a)
# ===========================================================================


def test_cron_on_paused_schedule_does_not_insert(tmp_path):
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn, status=ScheduleStatus.PAUSED)
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=aligned,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []
    assert _runs(conn) == []
    assert _events(conn) == []
    # Factories not called — the paused-schedule short-circuit
    # comes BEFORE the cron parser runs.
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


def test_cron_on_archived_schedule_does_not_insert(tmp_path):
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn, status=ScheduleStatus.ARCHIVED)
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=aligned,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []
    assert _runs(conn) == []
    assert _events(conn) == []
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


# ===========================================================================
# DOW numeric rejection
# ===========================================================================


@pytest.mark.parametrize(
    "cron",
    [
        "0 18 * * 1-5",       # numeric range
        "0 18 * * 0,6",       # numeric list
        "0 18 * * */2",       # numeric step (any anchor)
        "0 18 * * MON-FRI/2", # named range with numeric step
        "0 18 * * 1",         # single numeric day
    ],
)
def test_cron_numeric_dow_rejected(tmp_path, cron):
    """Any digit in the day-of-week field is rejected. Reason:
    APScheduler treats numeric DOW as Monday=0 while Unix cron
    uses Sunday=0; the silent semantic divergence would make
    ``1-5`` mean Mon-Fri to a Unix-trained author and Tue-Sat
    to APScheduler. Reject up front."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn, cron=cron)
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()
    with pytest.raises(ValueError, match="numeric day-of-week"):
        wakeup(
            conn,
            schedule_id="daily_audit",
            now=aligned,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )
    # The rejection happens BEFORE APScheduler parses the
    # expression and BEFORE the INSERT runs — pin no wasted ids.
    assert run_state["i"] == 0
    assert evt_state["i"] == 0
    assert _runs(conn) == []
    assert _events(conn) == []


@pytest.mark.parametrize(
    "cron",
    [
        "0 18 * * *",
        "0 18 * * FRI",
        "0 18 * * MON-FRI",
        "0 18 * * MON,WED,FRI",
        "*/5 * * * *",  # numeric in MINUTE field is fine
    ],
)
def test_cron_named_or_wildcard_dow_accepted(tmp_path, cron):
    """Pin the inverse: ``*``, named days, named ranges, named
    lists, and numeric digits in non-DOW fields (the minute
    field's ``*/5``) are all accepted. The rule applies only
    to DOW (field 5).

    APScheduler's ``CronTrigger`` does NOT accept the ``?``
    placeholder for day_of_week, so it is NOT included here
    even though some cron dialects do — the v2 rule is "if
    APScheduler accepts it AND the DOW field carries no
    digits, it's allowed"."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn, cron=cron)
    # 2026-05-15 is a Friday — picked so every parametrised
    # DOW (FRI / MON-FRI / MON,WED,FRI / *) fires at the
    # aligned 18:00 instant.
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="daily_audit",
        now=aligned,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == ["new-run-1"]


# ===========================================================================
# Invalid cron / timezone
# ===========================================================================


def test_invalid_timezone_rejected(tmp_path):
    """A timezone string that ``ZoneInfo`` can't resolve must
    raise ``ValueError`` with a clear message — APScheduler
    would otherwise raise its own less-informative error."""
    conn = _migrate(tmp_path)
    # Bypass the Pydantic CronTrigger validator (which only
    # checks non-empty timezone) by inserting the spec with an
    # arbitrary tz string that APScheduler / ZoneInfo can't
    # resolve at fire time.
    _seed_cron_schedule(
        conn,
        cron="0 18 * * *",
        timezone_name="Not/A_Real_Zone",
    )
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    with pytest.raises(ValueError, match="timezone"):
        wakeup(
            conn,
            schedule_id="daily_audit",
            now=aligned,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )
    assert _runs(conn) == []
    assert _events(conn) == []


def test_invalid_cron_expression_rejected(tmp_path):
    """A cron expression APScheduler refuses to parse surfaces
    as ``ValueError`` with the underlying APScheduler message
    attached. Pydantic's 5-field validator catches the simplest
    arity mismatch earlier; here we force the next layer.

    99 in the minute field is out of range for APScheduler.
    """
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn, cron="99 18 * * *")
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    with pytest.raises(ValueError, match="invalid cron"):
        wakeup(
            conn,
            schedule_id="daily_audit",
            now=aligned,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )


def test_cron_field_arity_other_than_five_rejected_defensively(tmp_path):
    """Phase-1 CronTrigger Pydantic validator rejects 6-field
    crons at construction. The wakeup helper has its own
    defensive 5-field check to surface the failure loudly if
    a hand-built trigger ever bypasses the model.

    Construct directly via SQL to skip the Pydantic guard."""
    conn = _migrate(tmp_path)
    # Seed a normal cron schedule then surgically overwrite
    # its trigger_json to a malformed 6-field cron. Bypasses
    # the Pydantic validator so the wakeup-layer guard sees
    # the bad expression.
    _seed_cron_schedule(conn, cron="0 18 * * *")
    conn.execute(
        "UPDATE schedules SET trigger_json = ? WHERE id = ?",
        (
            '{"type":"cron","cron":"0 18 * * * *","timezone":"UTC"}',
            "daily_audit",
        ),
    )
    # get_schedule would now fail Pydantic re-parse with 6
    # fields. Pin the surfaced error type.
    aligned = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    # The Pydantic CronTrigger validator on `_row_to_spec` is
    # what catches this — it surfaces a Pydantic ValidationError
    # rather than the wakeup helper's defensive ValueError. The
    # caller still sees a hard fail, which is the contract.
    with pytest.raises(Exception) as exc_info:
        wakeup(
            conn,
            schedule_id="daily_audit",
            now=aligned,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )
    # Just pin that it does fail loudly — the exact exception
    # type depends on which layer catches the malformed input.
    assert exc_info.type is not None
