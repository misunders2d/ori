"""Tests for ``app.v2.runtime.wakeup`` — slice 5a (OneOff only).

Pins per ``docs/PHASE_4_PLAN.md`` §6.2 / §6.5.

Behavioural coverage (slice 5a):
- OneOff trigger with ``at_iso_datetime > now`` → no insert,
  returns ``[]``.
- OneOff trigger with ``at_iso_datetime <= now`` → insert one
  pending Run + matching run_created event in the same TX;
  Run.due_at = trigger.at_iso_datetime; event.ts = wakeup
  ``now``; event payload carries ``fire_at`` + ``trigger_type``.
- OneOff trigger exactly at ``now`` (== boundary) → fires.
- Paused schedule + OneOff (any timing) → no insert.
- Archived schedule + OneOff (any timing) → no insert.
- Unknown schedule id → no insert, returns ``[]``.
- CronTrigger schedule → raises ``NotImplementedError`` with
  a slice-5b pointer.
- IntervalTrigger / EventTrigger / ConditionalTrigger →
  raise ``NotImplementedError`` with explicit per-type
  message.

Injection + structural pins:
- ``run_id_factory`` / ``event_id_factory`` called exactly
  once per inserted row (zero when nothing fires).
- Naive ``now`` raises ``NaiveDatetimeError`` before any SQL.
- ``assert_connection_ready`` is called.
- Module does NOT import ``uuid``.
- Module has no I/O / reasoning / emit callables.
- Atomicity: pre-seeded duplicate event_id makes the event
  INSERT raise; the Run INSERT rolls back; nothing landed.

Slice-5a scope guards:
- Wakeup raises for CronTrigger (5b not yet shipped).
- Wakeup raises for the three other trigger types.
"""

from __future__ import annotations

import inspect
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
from app.v2.models.triggers import (
    ConditionalTrigger,
    CronTrigger,
    EventTrigger,
    IntervalTrigger,
    OneOffTrigger,
)
from app.v2.runtime import wakeup as wakeup_mod
from app.v2.runtime.wakeup import wakeup
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.schedules import insert_schedule
from app.v2.storage.serialization import NaiveDatetimeError


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
_NOW_ISO = _NOW.isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_oneoff_schedule(
    conn: sqlite3.Connection,
    *,
    schedule_id: str = "morning_reminder",
    fire_at: datetime,
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
) -> None:
    spec = ScheduleSpec(
        id=schedule_id,
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="one-off reminder for testing",
        trigger=OneOffTrigger(
            at_iso_datetime=fire_at,
            timezone="UTC",
        ),
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


def _seed_cron_schedule(
    conn: sqlite3.Connection,
    *,
    schedule_id: str = "daily_audit",
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
) -> None:
    spec = ScheduleSpec(
        id=schedule_id,
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="daily audit cron",
        trigger=CronTrigger(cron="0 18 * * *", timezone="UTC"),
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


def _seed_interval_schedule(conn: sqlite3.Connection) -> None:
    spec = ScheduleSpec(
        id="interval_sched",
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="interval trigger schedule",
        trigger=IntervalTrigger(every_seconds=60),
        delivery=Delivery(
            target_session_id="sl_test",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
        execution_plan_hash=None,
        authored_at=_NOW_ISO,
    ).with_fresh_hash()
    insert_schedule(conn, spec)


def _seed_event_schedule(conn: sqlite3.Connection) -> None:
    spec = ScheduleSpec(
        id="event_sched",
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="event trigger schedule",
        trigger=EventTrigger(event="custom_signal"),
        delivery=Delivery(
            target_session_id="sl_test",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
        execution_plan_hash=None,
        authored_at=_NOW_ISO,
    ).with_fresh_hash()
    insert_schedule(conn, spec)


def _seed_conditional_schedule(conn: sqlite3.Connection) -> None:
    spec = ScheduleSpec(
        id="cond_sched",
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="conditional trigger schedule",
        trigger=ConditionalTrigger(gate="some_gate", poll_seconds=30),
        delivery=Delivery(
            target_session_id="sl_test",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
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
# OneOff happy paths
# ===========================================================================


def test_oneoff_in_the_past_inserts_one_run(tmp_path):
    """Past at_iso_datetime → wakeup inserts one pending Run
    + matching run_created event in the same TX."""
    conn = _migrate(tmp_path)
    fire_at = _NOW - timedelta(minutes=10)
    _seed_oneoff_schedule(conn, fire_at=fire_at)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()

    inserted = wakeup(
        conn,
        schedule_id="morning_reminder",
        now=_NOW,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == ["new-run-1"]

    runs = _runs(conn)
    assert len(runs) == 1
    row = runs[0]
    assert row["id"] == "new-run-1"
    assert row["schedule_id"] == "morning_reminder"
    assert row["fire_reason"] == FireReason.SCHEDULED.value
    assert row["status"] == RunStatus.PENDING.value
    assert row["attempt"] == 1
    assert row["root_run_id"] == "new-run-1"  # self-ref first attempt
    assert row["parent_run_id"] is None
    assert row["due_at"] == fire_at.isoformat()

    events = _events(conn)
    assert len(events) == 1
    evt = events[0]
    assert evt["id"] == "new-evt-1"
    assert evt["run_id"] == "new-run-1"
    assert evt["schedule_id"] == "morning_reminder"
    assert evt["kind"] == EventKind.RUN_CREATED.value
    assert evt["ts"] == _NOW.isoformat()
    assert evt["correlates"] is None
    assert '"fire_at":"' + fire_at.isoformat() + '"' in evt["payload_json"]
    assert '"trigger_type":"one_off"' in evt["payload_json"]

    # Factories invoked exactly once each.
    assert run_state["i"] == 1
    assert evt_state["i"] == 1


def test_oneoff_exactly_at_now_boundary_fires(tmp_path):
    """``at_iso_datetime == now`` is "due" per the plan's <=
    semantics. Pin the boundary explicitly so a refactor that
    flips to ``<`` regresses loudly."""
    conn = _migrate(tmp_path)
    _seed_oneoff_schedule(conn, fire_at=_NOW)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="morning_reminder",
        now=_NOW,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == ["new-run-1"]


def test_oneoff_in_the_future_no_insert(tmp_path):
    conn = _migrate(tmp_path)
    future = _NOW + timedelta(hours=1)
    _seed_oneoff_schedule(conn, fire_at=future)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()

    inserted = wakeup(
        conn,
        schedule_id="morning_reminder",
        now=_NOW,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []
    assert _runs(conn) == []
    assert _events(conn) == []
    # Factories not invoked when nothing fires.
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


def test_oneoff_with_non_utc_datetime_stored_as_utc(tmp_path):
    """Trigger ``at_iso_datetime`` stays whatever Pydantic
    parsed it as, but the storage layer normalises to UTC for
    the Run row's due_at field. Pin the UTC offset suffix."""
    conn = _migrate(tmp_path)
    eastern = datetime(
        2026, 5, 15, 12, 0, tzinfo=timezone(timedelta(hours=3))
    )
    _seed_oneoff_schedule(conn, fire_at=eastern)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    wakeup(
        conn,
        schedule_id="morning_reminder",
        now=_NOW,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    runs = _runs(conn)
    assert len(runs) == 1
    assert runs[0]["due_at"].endswith("+00:00")
    events = _events(conn)
    assert events[0]["ts"].endswith("+00:00")


# ===========================================================================
# Schedule-level no-ops
# ===========================================================================


def test_paused_schedule_does_not_insert(tmp_path):
    conn = _migrate(tmp_path)
    _seed_oneoff_schedule(
        conn,
        fire_at=_NOW - timedelta(minutes=10),
        status=ScheduleStatus.PAUSED,
    )
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()

    inserted = wakeup(
        conn,
        schedule_id="morning_reminder",
        now=_NOW,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []
    assert _runs(conn) == []
    assert _events(conn) == []
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


def test_archived_schedule_does_not_insert(tmp_path):
    conn = _migrate(tmp_path)
    _seed_oneoff_schedule(
        conn,
        fire_at=_NOW - timedelta(minutes=10),
        status=ScheduleStatus.ARCHIVED,
    )
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()

    inserted = wakeup(
        conn,
        schedule_id="morning_reminder",
        now=_NOW,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []
    assert _runs(conn) == []
    assert _events(conn) == []
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


def test_unknown_schedule_id_returns_empty(tmp_path):
    conn = _migrate(tmp_path)
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()
    inserted = wakeup(
        conn,
        schedule_id="ghost",
        now=_NOW,
        run_id_factory=run_factory,
        event_id_factory=evt_factory,
    )
    assert inserted == []
    assert _runs(conn) == []
    assert _events(conn) == []
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


# ===========================================================================
# Unwired trigger types (slice 5a scope guards)
# ===========================================================================


def test_cron_trigger_raises_not_implemented(tmp_path):
    """Slice 5a does NOT ship cron. The wakeup must raise
    NotImplementedError pointing at slice 5b, not silently
    no-op."""
    conn = _migrate(tmp_path)
    _seed_cron_schedule(conn)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    with pytest.raises(NotImplementedError, match="5b"):
        wakeup(
            conn,
            schedule_id="daily_audit",
            now=_NOW,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )
    # Nothing inserted.
    assert _runs(conn) == []
    assert _events(conn) == []


def test_interval_trigger_raises_not_implemented(tmp_path):
    conn = _migrate(tmp_path)
    _seed_interval_schedule(conn)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    with pytest.raises(NotImplementedError, match="interval"):
        wakeup(
            conn,
            schedule_id="interval_sched",
            now=_NOW,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )
    assert _runs(conn) == []
    assert _events(conn) == []


def test_event_trigger_raises_not_implemented(tmp_path):
    conn = _migrate(tmp_path)
    _seed_event_schedule(conn)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    with pytest.raises(NotImplementedError, match="event"):
        wakeup(
            conn,
            schedule_id="event_sched",
            now=_NOW,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )


def test_conditional_trigger_raises_not_implemented(tmp_path):
    conn = _migrate(tmp_path)
    _seed_conditional_schedule(conn)
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    with pytest.raises(NotImplementedError, match="conditional"):
        wakeup(
            conn,
            schedule_id="cond_sched",
            now=_NOW,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )


# ===========================================================================
# Datetime + connection guards
# ===========================================================================


def test_wakeup_rejects_naive_now(tmp_path):
    conn = _migrate(tmp_path)
    _seed_oneoff_schedule(conn, fire_at=_NOW - timedelta(minutes=10))
    run_factory, run_state = _run_counter()
    evt_factory, evt_state = _evt_counter()
    naive = datetime(2026, 5, 15, 9, 0)
    with pytest.raises(NaiveDatetimeError, match="now"):
        wakeup(
            conn,
            schedule_id="morning_reminder",
            now=naive,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )
    # Side-effect free: no rows, no factory calls.
    assert _runs(conn) == []
    assert _events(conn) == []
    assert run_state["i"] == 0
    assert evt_state["i"] == 0


def test_wakeup_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    run_factory, _ = _run_counter()
    evt_factory, _ = _evt_counter()
    with pytest.raises(ConnectionNotReady):
        wakeup(
            bare,
            schedule_id="ghost",
            now=_NOW,
            run_id_factory=run_factory,
            event_id_factory=evt_factory,
        )


# ===========================================================================
# Atomicity: Run insert rolls back if event insert fails
# ===========================================================================


def test_event_insert_failure_rolls_back_run_insert(tmp_path):
    """Force the event INSERT to fail by pre-seeding a row
    with the event id wakeup will use. The Run INSERT must
    roll back; neither row survives. Same atomicity guarantee
    claim_run / scan_stale_runs rely on."""
    conn = _migrate(tmp_path)
    fire_at = _NOW - timedelta(minutes=10)
    _seed_oneoff_schedule(conn, fire_at=fire_at)

    # First seed a Run row so we can reference it from a
    # pre-existing event (the events.run_id FK needs a real
    # run to point at — or NULL).
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, "
        " attempt, root_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "decoy-run",
            "morning_reminder",
            "scheduled",
            _NOW.isoformat(),
            "succeeded",
            1,
            "decoy-run",
        ),
    )
    # Pre-seed an event with the id wakeup will try to use.
    conn.execute(
        "INSERT INTO events "
        "(id, run_id, schedule_id, ts, kind, payload_json, correlates) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "evt-collide",
            "decoy-run",
            "morning_reminder",
            _NOW.isoformat(),
            "run_created",
            "{}",
            None,
        ),
    )

    def run_factory():
        return "new-run-1"

    def event_factory():
        return "evt-collide"

    with pytest.raises(sqlite3.IntegrityError):
        wakeup(
            conn,
            schedule_id="morning_reminder",
            now=_NOW,
            run_id_factory=run_factory,
            event_id_factory=event_factory,
        )

    # The new run did NOT survive — TX rolled back.
    runs = _runs(conn)
    assert {r["id"] for r in runs} == {"decoy-run"}
    # Pre-existing event survives; no new event landed.
    events = _events(conn)
    assert {e["id"] for e in events} == {"evt-collide"}


# ===========================================================================
# Hygiene smoke
# ===========================================================================


def test_wakeup_module_does_not_import_uuid():
    seen = set()
    for _, member in vars(wakeup_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


def test_wakeup_module_has_no_io_imports():
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "smtplib",
        "subprocess",
    }
    seen = set()
    for _, member in vars(wakeup_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"wakeup module imports I/O libs: {sorted(leaked)}."
    )


def test_wakeup_module_exposes_no_reasoning_or_emit_callables():
    forbidden = {
        "reason",
        "emit",
        "delegate",
        "transfer",
        "sub_agent",
        "dispatch",
        "invoke",
    }
    for name, member in vars(wakeup_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"wakeup module exposes execution-suggestive "
                f"callable: {name}"
            )


def test_wakeup_module_source_has_no_datetime_now_or_uuid_calls():
    """Same pin as the worker module — every timestamp comes
    from the caller's ``now``, every new id comes from a
    factory. No implicit calls allowed."""
    source = inspect.getsource(wakeup_mod)
    assert "datetime.now(" not in source, (
        "wakeup module must use the injected ``now`` arg, "
        "not datetime.now()."
    )
    assert "uuid.uuid4(" not in source, (
        "wakeup module must use the injected id factories, "
        "not uuid.uuid4()."
    )
