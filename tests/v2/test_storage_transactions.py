"""Tests for ``app.v2.storage.transactions``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.3:

- ``transaction()`` clean exit commits.
- Exception inside the block rolls back and re-raises.
- Partial mutation in a rolled-back TX leaves the DB untouched.
- Nested invocation raises (sqlite3 refuses an inner BEGIN).
- ``update_run_status_and_append_event`` is atomic on failure:
  a SQL error on the event INSERT rolls back the run UPDATE.
- The helper raises ``RunNotFoundError`` when the target run
  id has no row.
- The helper is policy-free: works for a generic
  ``running → succeeded`` transition. The plan explicitly
  forbids exercising ``pending → claimed`` here; that's
  phase 4 state-machine territory.
- Naive datetime values in ``event.ts`` or ``extra_columns``
  are rejected with ``NaiveDatetimeError``.

Cross-cutting smoke:
- Module imports no I/O libs.
- No execution-suggestive public callables.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import EventKind, RunStatus
from app.v2.migrations import runner
from app.v2.models.event import Event
from app.v2.storage import transactions as transactions_mod
from app.v2.storage.serialization import NaiveDatetimeError
from app.v2.storage.transactions import (
    EventRunMismatchError,
    RunNotFoundError,
    UnknownExtraColumnError,
    transaction,
    update_run_status_and_append_event,
)


# ---------------------------------------------------------------------------
# Fixtures: ephemeral DB + seeded schedule/run rows the helpers can target.
# ---------------------------------------------------------------------------


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
            "sergey@mellanni.com",
            "test schedule",
            "{}",
            "{}",
            "{}",
            "{}",
            "active",
            _NOW.isoformat(),
            f"hash-{schedule_id}",
        ),
    )


def _seed_run(
    conn: sqlite3.Connection,
    *,
    run_id: str = "run-abc",
    schedule_id: str = "daily_audit",
    status: str = "running",
) -> None:
    conn.execute(
        "INSERT INTO runs "
        "(id, schedule_id, fire_reason, due_at, status, attempt, "
        " root_run_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            schedule_id,
            "scheduled",
            _NOW.isoformat(),
            status,
            1,
            run_id,  # first-attempt self-ref
        ),
    )


def _make_event(
    *,
    event_id: str = "evt-1",
    run_id: str = "run-abc",
    schedule_id: str = "daily_audit",
    ts: datetime = _NOW,
    kind: EventKind = EventKind.RUN_SUCCEEDED,
    payload: dict | None = None,
) -> Event:
    return Event(
        id=event_id,
        run_id=run_id,
        schedule_id=schedule_id,
        ts=ts,
        kind=kind,
        payload=payload if payload is not None else {"reason": "ok"},
    )


# ===========================================================================
# transaction()
# ===========================================================================


def test_transaction_commits_on_clean_exit(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    with transaction(conn):
        conn.execute(
            "UPDATE runs SET status = 'succeeded' WHERE id = ?",
            ("run-abc",),
        )

    row = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    assert row[0] == "succeeded"


def test_transaction_rolls_back_on_exception(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, status="running")

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'succeeded' WHERE id = ?",
                ("run-abc",),
            )
            raise _Boom("deliberate")

    row = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    # ROLLBACK undoes the in-flight UPDATE.
    assert row[0] == "running"


def test_transaction_rollback_handles_partial_writes(tmp_path):
    """A more thorough partial-write scenario: two writes
    succeed, then a third raises. All three roll back."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="r1")
    _seed_run(conn, run_id="r2")

    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'succeeded' WHERE id = ?",
                ("r1",),
            )
            conn.execute(
                "UPDATE runs SET status = 'succeeded' WHERE id = ?",
                ("r2",),
            )
            # Force an integrity error: insert event with bad kind.
            conn.execute(
                "INSERT INTO events "
                "(id, run_id, schedule_id, ts, kind, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("e", "r1", "daily_audit", _NOW.isoformat(),
                 "not_a_real_kind", "{}"),
            )

    # Both UPDATEs rolled back.
    rows = conn.execute(
        "SELECT id, status FROM runs ORDER BY id"
    ).fetchall()
    assert rows == [("r1", "running"), ("r2", "running")]


def test_transaction_re_raises_original_exception(tmp_path):
    conn = _migrate(tmp_path)

    class _Specific(ValueError):
        pass

    with pytest.raises(_Specific, match="exact text"):
        with transaction(conn):
            raise _Specific("exact text")


def test_transaction_rejects_nested_use(tmp_path):
    """SQLite refuses an inner BEGIN when one is already open.
    The plan's "caller must not already be in a transaction"
    rule is enforced by sqlite itself; this test pins that
    behavior so a future refactor doesn't silently introduce
    SAVEPOINT-based nesting."""
    conn = _migrate(tmp_path)
    with transaction(conn):
        with pytest.raises(sqlite3.OperationalError, match="transaction"):
            with transaction(conn):
                pass


def test_transaction_does_not_commit_outer_when_entering_inner(tmp_path):
    """Regression pin against a sharp edge in Python's sqlite3:
    assigning ``conn.isolation_level = None`` (even with the same
    None value) silently COMMITs any active transaction. Our
    helper guards by only assigning when the value would change,
    so a nested ``transaction(conn)`` call surfaces the SQLite
    error rather than silently closing the parent TX.

    The test triggers the nested-entry path AND verifies the
    parent TX still rolls back its writes on parent-side
    exception — which means the parent TX wasn't auto-committed
    by the inner's entry."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'succeeded' WHERE id = ?",
                ("run-abc",),
            )
            # Inner call raises immediately; outer TX should still
            # be active and roll back the UPDATE above.
            with pytest.raises(sqlite3.OperationalError, match="transaction"):
                with transaction(conn):
                    pass
            raise _Boom("verify outer TX still active + rolls back")

    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    # If the inner's isolation_level assignment had silently
    # committed the parent TX, status would be 'succeeded'.
    assert status == "running"


def test_transaction_keyboard_interrupt_rolls_back(tmp_path):
    """``BaseException`` catch handles KeyboardInterrupt /
    SystemExit, not just regular exceptions."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    with pytest.raises(KeyboardInterrupt):
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET status = 'succeeded' WHERE id = ?",
                ("run-abc",),
            )
            raise KeyboardInterrupt

    row = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    assert row[0] == "running"


# ===========================================================================
# update_run_status_and_append_event — happy path
# ===========================================================================


def test_helper_writes_both_rows_atomically(tmp_path):
    """Use a neutral non-claim transition (running → succeeded).
    Pinning policy-freedom: the helper accepts the unpredicated
    update regardless of source status."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, status="running")

    update_run_status_and_append_event(
        conn,
        run_id="run-abc",
        new_status=RunStatus.SUCCEEDED,
        event=_make_event(kind=EventKind.RUN_SUCCEEDED),
    )

    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert status == "succeeded"

    event_kind = conn.execute(
        "SELECT kind FROM events WHERE id = 'evt-1'"
    ).fetchone()[0]
    assert event_kind == "run_succeeded"


def test_helper_handles_extra_columns(tmp_path):
    """Common usage: setting started_at / completed_at /
    error alongside the status change."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    completed_at = _NOW + timedelta(minutes=5)
    update_run_status_and_append_event(
        conn,
        run_id="run-abc",
        new_status=RunStatus.SUCCEEDED,
        event=_make_event(),
        extra_columns={
            "completed_at": completed_at,
            "error": None,
        },
    )

    row = conn.execute(
        "SELECT status, completed_at, error FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    assert row[0] == "succeeded"
    assert row[1] == completed_at.isoformat()
    assert row[2] is None


def test_helper_serializes_payload_dict(tmp_path):
    """The event payload is encoded via encode_json (sort_keys
    + compact separators); pin that the on-wire bytes are the
    deterministic form."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    update_run_status_and_append_event(
        conn,
        run_id="run-abc",
        new_status=RunStatus.SUCCEEDED,
        event=_make_event(payload={"b": 2, "a": 1}),
    )
    payload = conn.execute(
        "SELECT payload_json FROM events WHERE id = 'evt-1'"
    ).fetchone()[0]
    assert payload == '{"a":1,"b":2}'


# ===========================================================================
# update_run_status_and_append_event — error paths
# ===========================================================================


def test_helper_raises_when_run_id_missing(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)

    with pytest.raises(RunNotFoundError, match="ghost-run"):
        update_run_status_and_append_event(
            conn,
            run_id="ghost-run",
            new_status=RunStatus.SUCCEEDED,
            event=_make_event(run_id="ghost-run"),
        )


def test_helper_rolls_back_when_event_insert_fails(tmp_path):
    """The classic atomicity property: UPDATE succeeds, INSERT
    fails (here we pre-seed a duplicate event id to trigger a
    PK collision on the second insert). Both must roll back."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    # Pre-seed an event row that the helper's INSERT will
    # collide with on PK.
    conn.execute(
        "INSERT INTO events "
        "(id, run_id, schedule_id, ts, kind, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("evt-1", "run-abc", "daily_audit", _NOW.isoformat(),
         "run_started", "{}"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        update_run_status_and_append_event(
            conn,
            run_id="run-abc",
            new_status=RunStatus.SUCCEEDED,
            event=_make_event(kind=EventKind.RUN_SUCCEEDED),
        )

    # Run status was NOT changed — the UPDATE rolled back with
    # the failed INSERT.
    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert status == "running"


def test_helper_rejects_naive_event_ts(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    naive_dt = datetime(2026, 5, 15, 9, 0)
    naive_event = _make_event(ts=naive_dt)

    with pytest.raises(NaiveDatetimeError, match="event.ts"):
        update_run_status_and_append_event(
            conn,
            run_id="run-abc",
            new_status=RunStatus.SUCCEEDED,
            event=naive_event,
        )

    # Run status untouched.
    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert status == "running"


def test_helper_rejects_naive_datetime_in_extra_columns(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    naive_dt = datetime(2026, 5, 15, 9, 5)
    with pytest.raises(NaiveDatetimeError, match="extra_columns"):
        update_run_status_and_append_event(
            conn,
            run_id="run-abc",
            new_status=RunStatus.SUCCEEDED,
            event=_make_event(),
            extra_columns={"completed_at": naive_dt},
        )

    # Both writes never happened.
    row = conn.execute(
        "SELECT status, completed_at FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    assert row[0] == "running"
    assert row[1] is None


def test_helper_calls_assert_connection_ready(tmp_path):
    """Bare connection with no migration → helper refuses at
    function entry, before any UPDATE / INSERT runs."""
    from app.v2.storage.connection import ConnectionNotReady

    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    # No migration, no pragmas.
    with pytest.raises(ConnectionNotReady):
        update_run_status_and_append_event(
            bare,
            run_id="any",
            new_status=RunStatus.SUCCEEDED,
            event=_make_event(),
        )


# ===========================================================================
# Event ↔ run / schedule consistency (reviewer follow-up).
# The helper must refuse to record an event that names a
# different run or a different schedule than the row being
# updated — otherwise the audit ledger can claim run B changed
# when run A is what actually moved.
# ===========================================================================


def test_helper_rejects_event_pointing_at_wrong_run(tmp_path):
    """``event.run_id`` != the function arg ``run_id`` →
    ``EventRunMismatchError``. Caught BEFORE any DB write —
    asserts the run stays in its original status."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="run-abc")
    _seed_run(conn, run_id="run-other")

    other_run_event = _make_event(run_id="run-other")
    with pytest.raises(
        EventRunMismatchError, match="event.run_id"
    ):
        update_run_status_and_append_event(
            conn,
            run_id="run-abc",
            new_status=RunStatus.SUCCEEDED,
            event=other_run_event,
        )

    # Neither run changed.
    rows = conn.execute(
        "SELECT id, status FROM runs ORDER BY id"
    ).fetchall()
    assert rows == [("run-abc", "running"), ("run-other", "running")]


def test_helper_rejects_event_pointing_at_wrong_schedule(tmp_path):
    """``event.schedule_id`` != the updated run's
    ``schedule_id`` → ``EventRunMismatchError``. The check
    happens INSIDE the TX (after fetching the run's actual
    schedule_id), so the run UPDATE never runs."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, schedule_id="daily_audit")
    _seed_schedule(conn, schedule_id="weekly_report")
    _seed_run(conn, run_id="run-abc", schedule_id="daily_audit")

    cross_schedule_event = _make_event(
        run_id="run-abc",
        schedule_id="weekly_report",  # WRONG: run belongs to daily_audit
    )

    with pytest.raises(
        EventRunMismatchError, match="schedule_id"
    ):
        update_run_status_and_append_event(
            conn,
            run_id="run-abc",
            new_status=RunStatus.SUCCEEDED,
            event=cross_schedule_event,
        )

    # The UPDATE never ran — the in-TX SELECT caught the
    # mismatch first.
    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert status == "running"
    # And no event row leaked.
    event_count = conn.execute(
        "SELECT COUNT(*) FROM events"
    ).fetchone()[0]
    assert event_count == 0


def test_helper_accepts_matching_run_and_schedule(tmp_path):
    """Positive control: when both ids match the helper writes
    both rows normally — no false-positive consistency error."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, schedule_id="daily_audit")
    _seed_run(conn, run_id="run-abc", schedule_id="daily_audit")

    update_run_status_and_append_event(
        conn,
        run_id="run-abc",
        new_status=RunStatus.SUCCEEDED,
        event=_make_event(run_id="run-abc", schedule_id="daily_audit"),
    )

    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert status == "succeeded"


# ===========================================================================
# extra_columns allowlist (reviewer follow-up).
# Keys go straight into the UPDATE identifier list, so the
# helper accepts ONLY a fixed phase-3 allowlist. Anything else
# raises UnknownExtraColumnError before any DB write.
# ===========================================================================


@pytest.mark.parametrize(
    "key",
    ["started_at", "completed_at", "error"],
)
def test_helper_accepts_allowed_extra_columns(tmp_path, key):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    value: object
    if key.endswith("_at"):
        value = _NOW + timedelta(minutes=1)
    else:
        value = "test error message"

    update_run_status_and_append_event(
        conn,
        run_id="run-abc",
        new_status=RunStatus.SUCCEEDED,
        event=_make_event(),
        extra_columns={key: value},
    )
    row = conn.execute(
        f"SELECT {key} FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    expected = value.isoformat() if isinstance(value, datetime) else value
    assert row[0] == expected


@pytest.mark.parametrize(
    "bad_key",
    [
        # Columns that would corrupt the schema if writeable.
        "status",
        "schedule_id",
        "attempt",
        "root_run_id",
        "parent_run_id",
        # Claim columns — phase 4 territory.
        "claimed_by",
        "claimed_at",
        # Bogus columns.
        "id",
        "fire_reason",
        # SQL injection attempts.
        "started_at = 'x'; DROP TABLE runs; --",
        "started_at, evil_col",
    ],
)
def test_helper_rejects_unknown_extra_columns(tmp_path, bad_key):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    with pytest.raises(UnknownExtraColumnError):
        update_run_status_and_append_event(
            conn,
            run_id="run-abc",
            new_status=RunStatus.SUCCEEDED,
            event=_make_event(),
            extra_columns={bad_key: "any-value"},
        )

    # Confirm nothing was mutated.
    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert status == "running"


def test_helper_rejects_extra_columns_with_mixed_allowed_and_unknown(tmp_path):
    """Even one bad key in an otherwise-valid set fails the
    whole call — fail-loud rather than partial-apply."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)

    with pytest.raises(UnknownExtraColumnError, match="claimed_by"):
        update_run_status_and_append_event(
            conn,
            run_id="run-abc",
            new_status=RunStatus.SUCCEEDED,
            event=_make_event(),
            extra_columns={
                "completed_at": _NOW,
                "claimed_by": "worker-1",  # phase 4 territory
            },
        )

    # completed_at was NOT applied.
    row = conn.execute(
        "SELECT status, completed_at FROM runs WHERE id = 'run-abc'"
    ).fetchone()
    assert row[0] == "running"
    assert row[1] is None


# ===========================================================================
# Policy-freedom pin: helper does not enforce source status.
# Plan explicitly forbids exercising pending → claimed here;
# this test confirms the helper would accept ANY transition,
# leaving policy to phase 4. Test uses a neutral transition
# (failed → cancelled) that has no claim semantics.
# ===========================================================================


def test_helper_does_not_validate_source_status(tmp_path):
    """An unpredicated UPDATE accepts even a contrived
    transition like ``failed → cancelled``. The helper is the
    data-plane atomicity guarantee; the state-machine policy
    lives in phase 4."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, status="failed")

    update_run_status_and_append_event(
        conn,
        run_id="run-abc",
        new_status=RunStatus.CANCELLED,
        event=_make_event(kind=EventKind.RUN_CANCELLED),
    )

    status = conn.execute(
        "SELECT status FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert status == "cancelled"


# ===========================================================================
# Smoke checks
# ===========================================================================


def test_transactions_module_has_no_io_imports():
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
    for _, member in vars(transactions_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"transactions module imports I/O libs: {sorted(leaked)}."
    )


def test_transactions_module_has_no_dispatch_callables():
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
    for name, member in vars(transactions_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"transactions module exposes execution / claim "
                f"suggestive callable: {name}"
            )
