"""Tests for ``app.v2.storage.events``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.7:

- ``append_event`` + ``list_events_for_run`` round-trip
  preserves every Event field.
- ``list_events_for_run`` orders ASC chronologically.
- ``list_events_for_schedule`` orders DESC (recent-first);
  ``kind`` filter narrows; ``limit`` caps.
- ``get_last_emit_succeeded`` finds the most recent matching
  payload; returns None when missing; only matches
  ``kind='emit_succeeded'`` (NOT ``emit_failed``).
- Naive ``event.ts`` rejected on append.
- Non-UTC ``event.ts`` stored as ``+00:00``.
- ``limit < 1`` rejected in ``list_events_for_schedule``.
- Module exposes no update/delete helper.

Smoke:
- No I/O imports.
- No execution / claim-suggestive public callables.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import EventKind
from app.v2.migrations import runner
from app.v2.models.event import Event
from app.v2.storage import events as events_mod
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.events import (
    append_event,
    get_last_emit_succeeded,
    list_events_for_run,
    list_events_for_schedule,
)
from app.v2.storage.serialization import NaiveDatetimeError
from app.v2.storage.transactions import EventRunMismatchError


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(conn: sqlite3.Connection, schedule_id: str = "daily_audit") -> None:
    conn.execute(
        "INSERT INTO schedules "
        "(id, owner, description, trigger_json, delivery_json, "
        " failure_json, audit_json, status, authored_at, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            schedule_id,
            "{}",
            "test",
            "{}",
            "{}",
            "{}",
            "{}",
            "active",
            _NOW.isoformat(),
            f"hash-{schedule_id}",
        ),
    )


def _make_event(
    *,
    event_id: str = "evt-1",
    run_id: str | None = None,
    schedule_id: str = "daily_audit",
    ts: datetime | None = None,
    kind: EventKind = EventKind.SCHEDULE_CREATED,
    payload: dict | None = None,
    correlates: str | None = None,
) -> Event:
    return Event(
        id=event_id,
        run_id=run_id,
        schedule_id=schedule_id,
        ts=ts if ts is not None else _NOW,
        kind=kind,
        payload=payload if payload is not None else {"reason": "ok"},
        correlates=correlates,
    )


# ===========================================================================
# append + list_events_for_run round-trip
# ===========================================================================


def test_append_event_with_run_id_requires_seeded_run(tmp_path):
    """events.run_id FK to runs.id. Without a matching row the
    INSERT raises IntegrityError. This is design-correct;
    test exists to pin the behavior."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    event = _make_event(run_id="missing-run")
    with pytest.raises(sqlite3.IntegrityError):
        append_event(conn, event)


def _seed_run(conn: sqlite3.Connection, run_id: str = "run-abc") -> None:
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, attempt, "
        " root_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "daily_audit",
            "scheduled",
            _NOW.isoformat(),
            "pending",
            1,
            run_id,
        ),
    )


def test_append_schedule_level_event_round_trip(tmp_path):
    """run_id IS NULL for schedule-level events; no FK row
    needed."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    event = _make_event(run_id=None, kind=EventKind.SCHEDULE_CREATED)
    append_event(conn, event)

    rows = list_events_for_schedule(conn, "daily_audit")
    assert len(rows) == 1
    assert rows[0] == event


def test_append_run_level_event_round_trip(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    event = _make_event(run_id="run-abc", kind=EventKind.RUN_STARTED)
    append_event(conn, event)

    fetched_list = list_events_for_run(conn, "run-abc")
    assert fetched_list == [event]


def test_round_trip_preserves_payload_dict(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    event = _make_event(payload={"nested": {"key": "value"}, "n": 7})
    append_event(conn, event)
    fetched = list_events_for_schedule(conn, "daily_audit")[0]
    assert fetched.payload == {"nested": {"key": "value"}, "n": 7}


def test_round_trip_preserves_correlates(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    seed = _make_event(event_id="seed", correlates=None)
    follow = _make_event(
        event_id="follow",
        correlates="seed",
        ts=_NOW + timedelta(seconds=1),
    )
    append_event(conn, seed)
    append_event(conn, follow)
    fetched = list_events_for_schedule(conn, "daily_audit", limit=10)
    by_id = {e.id: e for e in fetched}
    assert by_id["follow"].correlates == "seed"
    assert by_id["seed"].correlates is None


# ===========================================================================
# Run / schedule consistency (reviewer follow-up).
# Without this guard, both FK constraints could pass while the
# ledger row falsely attributes a run-A event to schedule B.
# ===========================================================================


def test_append_event_rejects_run_schedule_mismatch(tmp_path):
    """Two schedules; a run lives under schedule A; an event
    naming run-A but schedule-B must be refused with
    EventRunMismatchError — and no row leaks to the events
    table."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, schedule_id="daily_audit")
    _seed_schedule(conn, schedule_id="weekly_report")
    # Run lives under daily_audit.
    _seed_run(conn, run_id="run-abc")

    cross_schedule_event = _make_event(
        event_id="bad",
        run_id="run-abc",
        schedule_id="weekly_report",  # WRONG — run is under daily_audit
        kind=EventKind.RUN_STARTED,
    )

    with pytest.raises(EventRunMismatchError, match="schedule_id"):
        append_event(conn, cross_schedule_event)

    # Neither schedule got an event row.
    assert list_events_for_schedule(conn, "daily_audit") == []
    assert list_events_for_schedule(conn, "weekly_report") == []


def test_append_event_accepts_run_schedule_match(tmp_path):
    """Positive control: same data, but event.schedule_id
    matches the run's actual schedule. Helper writes normally
    — no false-positive on the guard."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, schedule_id="daily_audit")
    _seed_run(conn, run_id="run-abc")

    correct_event = _make_event(
        event_id="ok",
        run_id="run-abc",
        schedule_id="daily_audit",
        kind=EventKind.RUN_STARTED,
    )
    append_event(conn, correct_event)
    fetched = list_events_for_run(conn, "run-abc")
    assert [e.id for e in fetched] == ["ok"]


def test_append_event_skips_consistency_check_when_run_id_none(tmp_path):
    """Schedule-level events (run_id IS NULL) have no run to
    reconcile against. The guard must NOT fire — those are
    written all the time during schedule lifecycle."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, schedule_id="daily_audit")
    # No runs at all in the DB. Append a schedule-level event.
    append_event(
        conn,
        _make_event(run_id=None, kind=EventKind.SCHEDULE_CREATED),
    )
    assert len(list_events_for_schedule(conn, "daily_audit")) == 1


# ===========================================================================
# Ordering
# ===========================================================================


def test_list_events_for_run_orders_chronologically(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    e1 = _make_event(event_id="e1", run_id="run-abc", ts=_NOW)
    e2 = _make_event(
        event_id="e2",
        run_id="run-abc",
        ts=_NOW + timedelta(seconds=10),
    )
    e3 = _make_event(
        event_id="e3",
        run_id="run-abc",
        ts=_NOW + timedelta(seconds=5),
    )
    # Insert out of order; result must be chronological.
    append_event(conn, e1)
    append_event(conn, e2)
    append_event(conn, e3)
    fetched = list_events_for_run(conn, "run-abc")
    assert [e.id for e in fetched] == ["e1", "e3", "e2"]


def test_list_events_for_schedule_orders_recent_first(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    e_old = _make_event(event_id="old", ts=_NOW)
    e_new = _make_event(event_id="new", ts=_NOW + timedelta(seconds=10))
    append_event(conn, e_old)
    append_event(conn, e_new)
    fetched = list_events_for_schedule(conn, "daily_audit")
    assert [e.id for e in fetched] == ["new", "old"]


# ===========================================================================
# list_events_for_schedule filters
# ===========================================================================


def test_list_events_for_schedule_filters_by_kind(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    append_event(
        conn,
        _make_event(event_id="created", kind=EventKind.SCHEDULE_CREATED),
    )
    append_event(
        conn,
        _make_event(
            event_id="started",
            run_id="run-abc",
            kind=EventKind.RUN_STARTED,
            ts=_NOW + timedelta(seconds=1),
        ),
    )
    append_event(
        conn,
        _make_event(
            event_id="succeeded",
            run_id="run-abc",
            kind=EventKind.RUN_SUCCEEDED,
            ts=_NOW + timedelta(seconds=2),
        ),
    )

    succeeded_only = list_events_for_schedule(
        conn, "daily_audit", kind=EventKind.RUN_SUCCEEDED
    )
    assert [e.id for e in succeeded_only] == ["succeeded"]


def test_list_events_for_schedule_respects_limit(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    for i in range(10):
        append_event(
            conn,
            _make_event(
                event_id=f"e-{i}",
                ts=_NOW + timedelta(seconds=i),
            ),
        )
    fetched = list_events_for_schedule(conn, "daily_audit", limit=3)
    assert len(fetched) == 3
    # Recent-first — latest three indices.
    assert [e.id for e in fetched] == ["e-9", "e-8", "e-7"]


def test_list_events_for_schedule_isolates_per_schedule(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "daily_audit")
    _seed_schedule(conn, "weekly_report")
    append_event(
        conn,
        _make_event(event_id="daily", schedule_id="daily_audit"),
    )
    append_event(
        conn,
        _make_event(event_id="weekly", schedule_id="weekly_report"),
    )
    daily = list_events_for_schedule(conn, "daily_audit")
    assert [e.id for e in daily] == ["daily"]


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_events_for_schedule_rejects_non_positive_limit(tmp_path, bad_limit):
    conn = _migrate(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        list_events_for_schedule(conn, "any", limit=bad_limit)


def test_list_events_for_schedule_empty(tmp_path):
    conn = _migrate(tmp_path)
    assert list_events_for_schedule(conn, "unknown_id") == []


# ===========================================================================
# get_last_emit_succeeded
# ===========================================================================


def test_get_last_emit_succeeded_finds_matching(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    key = "daily_audit:root-abc:post_slack"
    event = _make_event(
        event_id="emit-ok",
        run_id="run-abc",
        kind=EventKind.EMIT_SUCCEEDED,
        payload={"idempotency_key": key, "destination_id": "1234.5"},
    )
    append_event(conn, event)

    fetched = get_last_emit_succeeded(conn, key)
    assert fetched is not None
    assert fetched.id == "emit-ok"


def test_get_last_emit_succeeded_returns_latest_match(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    key = "daily_audit:root-abc:post_slack"
    # Two emit_succeeded rows with same idempotency_key (would
    # only happen if dedup failed — but we want the helper to
    # return the LATEST so the audit trail leans on most-recent
    # truth).
    append_event(
        conn,
        _make_event(
            event_id="old",
            run_id="run-abc",
            kind=EventKind.EMIT_SUCCEEDED,
            payload={"idempotency_key": key},
            ts=_NOW,
        ),
    )
    append_event(
        conn,
        _make_event(
            event_id="new",
            run_id="run-abc",
            kind=EventKind.EMIT_SUCCEEDED,
            payload={"idempotency_key": key},
            ts=_NOW + timedelta(seconds=10),
        ),
    )
    fetched = get_last_emit_succeeded(conn, key)
    assert fetched.id == "new"


def test_get_last_emit_succeeded_returns_none_when_missing(tmp_path):
    conn = _migrate(tmp_path)
    assert get_last_emit_succeeded(conn, "no-such-key") is None


def test_get_last_emit_succeeded_does_not_match_emit_failed(tmp_path):
    """Critical correctness: an emit_failed event with the same
    idempotency_key MUST NOT count as a dedup hit. The kind
    filter is load-bearing."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    key = "daily_audit:root-abc:post_slack"
    append_event(
        conn,
        _make_event(
            event_id="fail",
            run_id="run-abc",
            kind=EventKind.EMIT_FAILED,
            payload={"idempotency_key": key, "error": "503"},
        ),
    )
    assert get_last_emit_succeeded(conn, key) is None


def test_get_last_emit_succeeded_ignores_other_payload_keys(tmp_path):
    """Lookup keys on the literal field name
    ``idempotency_key`` — a different shape payload doesn't
    spuriously match."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    append_event(
        conn,
        _make_event(
            event_id="other",
            run_id="run-abc",
            kind=EventKind.EMIT_SUCCEEDED,
            payload={"different_key": "x"},
        ),
    )
    assert get_last_emit_succeeded(conn, "x") is None


# ===========================================================================
# Datetime + UTC normalisation
# ===========================================================================


def test_append_rejects_naive_event_ts(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    naive = datetime(2026, 5, 15, 9, 0)
    naive_event = _make_event(ts=naive)
    with pytest.raises(NaiveDatetimeError, match="event.ts"):
        append_event(conn, naive_event)
    # And no row leaked.
    assert list_events_for_schedule(conn, "daily_audit") == []


def test_append_normalises_non_utc_to_utc(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    eastern = datetime(
        2026, 5, 15, 12, 0, tzinfo=timezone(timedelta(hours=3))
    )
    event = _make_event(ts=eastern)
    append_event(conn, event)
    stored = conn.execute(
        "SELECT ts FROM events WHERE id = 'evt-1'"
    ).fetchone()[0]
    assert stored.endswith("+00:00")
    # Round-tripped datetime equals the original moment.
    fetched = list_events_for_schedule(conn, "daily_audit")[0]
    assert fetched.ts == eastern


def test_recent_first_ordering_correct_across_offsets(tmp_path):
    """Mirror of the runs.py non-UTC regression: store events
    written in different offsets, verify the ORDER BY ts DESC
    surfaces them by actual chronological time."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    # 10:00+03:00 = 07:00Z; 08:00Z = 08:00Z; 09:00-01:00 = 10:00Z.
    early = datetime(2026, 5, 15, 10, 0, tzinfo=timezone(timedelta(hours=3)))
    middle = datetime(2026, 5, 15, 8, 0, tzinfo=timezone.utc)
    late = datetime(2026, 5, 15, 9, 0, tzinfo=timezone(timedelta(hours=-1)))
    append_event(conn, _make_event(event_id="early", ts=early))
    append_event(conn, _make_event(event_id="middle", ts=middle))
    append_event(conn, _make_event(event_id="late", ts=late))
    fetched = list_events_for_schedule(conn, "daily_audit", limit=10)
    assert [e.id for e in fetched] == ["late", "middle", "early"]


# ===========================================================================
# Append-only surface
# ===========================================================================


def test_module_exposes_no_update_or_delete_helper():
    public = {
        name for name in dir(events_mod) if not name.startswith("_")
    }
    forbidden = {
        "update_event",
        "delete_event",
        "modify_event",
        "edit_event",
        "drop_event",
    }
    leaked = public & forbidden
    assert not leaked, (
        f"events module exposes mutation helpers: {sorted(leaked)}. "
        "Ledger is append-only at the API surface."
    )


# ===========================================================================
# Connection guard
# ===========================================================================


def test_append_event_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        append_event(bare, _make_event())


def test_list_events_for_run_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        list_events_for_run(bare, "any")


def test_list_events_for_schedule_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        list_events_for_schedule(bare, "any")


def test_get_last_emit_succeeded_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        get_last_emit_succeeded(bare, "any-key")


# ===========================================================================
# Smoke
# ===========================================================================


def test_events_module_has_no_io_imports():
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
    for _, member in vars(events_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"events module imports I/O libs: {sorted(leaked)}."
    )


def test_events_module_has_no_dispatch_callables():
    forbidden = {
        "dispatch",
        "invoke",
        "call",
        "execute",
        "run",
        "send",
        "start",
        "loop",
        "worker",
        "claim",
        "claim_run",
    }
    for name, member in vars(events_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"events module exposes execution / claim-suggestive "
                f"callable: {name}"
            )
