"""Tests for ``app.v2.storage.runs``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.6:

- Insert + get round-trip (all fields incl. optional datetimes).
- ``list_pending_due`` orders by ``due_at`` ASC, respects
  ``limit``, excludes non-pending statuses.
- ``list_runs_in_chain`` returns chain in attempt order.
- ``mark_run_status`` writes status + extras; unpredicated
  (no source-status filter — pinned via failed → cancelled).
- ``mark_run_status`` raises ``RunNotFoundError`` on missing.
- ``mark_run_status`` rejects unknown extras via the same
  allowlist used by ``update_run_status_and_append_event``.
- ``mark_run_status`` rejects naive datetime extras.
- Naive datetime on ``insert_run.due_at`` rejected.
- Naive datetime on ``list_pending_due.now`` rejected.
- ``assert_connection_ready`` called per helper.
- Drift guard: runs allowlist == transactions allowlist.

**Explicitly forbidden in phase 3 tests:** ``pending →
claimed`` transition, single-flight scenarios, "two workers
race" simulations. Those land in phase 4 with the worker.

Smoke:
- Module imports no I/O libs.
- No execution / claim-suggestive public callables.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import FireReason, RunStatus
from app.v2.migrations import runner
from app.v2.models.run import Run
from app.v2.storage import runs as runs_mod
from app.v2.storage import transactions as transactions_mod
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.runs import (
    ALLOWED_EXTRA_RUN_COLUMNS,
    get_run,
    insert_run,
    list_pending_due,
    list_runs_in_chain,
    mark_run_status,
)
from app.v2.storage.serialization import NaiveDatetimeError
from app.v2.storage.transactions import (
    RunNotFoundError,
    UnknownExtraColumnError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(conn: sqlite3.Connection, schedule_id: str = "daily_audit") -> None:
    """Seed a schedule row so FK on runs.schedule_id is satisfied."""
    conn.execute(
        "INSERT INTO schedules "
        "(id, owner, description, trigger_json, delivery_json, "
        " failure_json, audit_json, status, authored_at, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            schedule_id,
            "{}",
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


def _baseline_run(**overrides) -> Run:
    """Construct a first-attempt pending run. Overrides extend
    or replace fields."""
    base = dict(
        id="run-abc",
        schedule_id="daily_audit",
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.PENDING,
        attempt=1,
        root_run_id="run-abc",  # self-ref
        parent_run_id=None,
    )
    base.update(overrides)
    return Run(**base)


# ===========================================================================
# Insert + get round-trip
# ===========================================================================


def test_insert_and_get_round_trip(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    run = _baseline_run()
    returned = insert_run(conn, run)
    assert returned == run.id

    fetched = get_run(conn, run.id)
    assert fetched is not None
    assert fetched == run


def test_round_trip_preserves_all_datetime_fields(tmp_path):
    """Pre-seed a run with started_at / completed_at populated
    (e.g. a fixture for replay tests). The phase-3 storage layer
    accepts these regardless of source status — phase 4 owns
    the claim-vs-run state machine."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    started = _NOW + timedelta(seconds=10)
    completed = _NOW + timedelta(seconds=42)
    run = _baseline_run(
        status=RunStatus.SUCCEEDED,
        started_at=started,
        completed_at=completed,
    )
    insert_run(conn, run)
    fetched = get_run(conn, run.id)
    assert fetched.started_at == started
    assert fetched.completed_at == completed


def test_round_trip_preserves_execution_plan_hash(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    # Seed an execution_plans row so the runs.execution_plan_hash
    # FK is satisfied — unlike schedules.execution_plan_hash, the
    # DDL declares the FK explicitly on the runs side.
    plan_hash = "a" * 64
    conn.execute(
        "INSERT INTO execution_plans "
        "(hash, body_json, enforcement, authored_at, author) "
        "VALUES (?, ?, ?, ?, ?)",
        (plan_hash, "{}", "strict", _NOW.isoformat(), "sergey"),
    )
    run = _baseline_run(execution_plan_hash=plan_hash)
    insert_run(conn, run)
    fetched = get_run(conn, run.id)
    assert fetched.execution_plan_hash == plan_hash


def test_round_trip_preserves_null_optional_fields(tmp_path):
    """A pending first-attempt run has every optional field
    (execution_plan_hash, parent_run_id, claimed_*, started_at,
    completed_at, error) at None. Pin the round-trip."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    run = _baseline_run()
    insert_run(conn, run)
    fetched = get_run(conn, run.id)
    assert fetched.execution_plan_hash is None
    assert fetched.parent_run_id is None
    assert fetched.claimed_by is None
    assert fetched.claimed_at is None
    assert fetched.started_at is None
    assert fetched.completed_at is None
    assert fetched.error is None


def test_insert_naive_due_at_rejected(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    naive = datetime(2026, 5, 15, 9, 0)
    # Build a Run with naive due_at via model_construct so Pydantic
    # doesn't auto-attach tzinfo.
    run = Run.model_construct(
        id="run-naive",
        schedule_id="daily_audit",
        execution_plan_hash=None,
        fire_reason=FireReason.SCHEDULED,
        due_at=naive,
        status=RunStatus.PENDING,
        attempt=1,
        root_run_id="run-naive",
        parent_run_id=None,
        claimed_by=None,
        claimed_at=None,
        started_at=None,
        completed_at=None,
        error=None,
    )
    with pytest.raises(NaiveDatetimeError, match="due_at"):
        insert_run(conn, run)


def test_get_missing_run_returns_none(tmp_path):
    conn = _migrate(tmp_path)
    assert get_run(conn, "ghost") is None


# ===========================================================================
# list_pending_due
# ===========================================================================


def test_list_pending_due_orders_by_due_at(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    later = _NOW + timedelta(minutes=10)
    earlier = _NOW - timedelta(minutes=5)
    insert_run(conn, _baseline_run(id="r-late", root_run_id="r-late", due_at=later))
    insert_run(
        conn, _baseline_run(id="r-early", root_run_id="r-early", due_at=earlier)
    )
    insert_run(conn, _baseline_run(id="r-now", root_run_id="r-now", due_at=_NOW))

    results = list_pending_due(conn, now=_NOW + timedelta(minutes=15), limit=10)
    assert [r.id for r in results] == ["r-early", "r-now", "r-late"]


def test_list_pending_due_excludes_non_pending(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run(id="r-pending", root_run_id="r-pending"))
    insert_run(
        conn,
        _baseline_run(
            id="r-running",
            root_run_id="r-running",
            status=RunStatus.RUNNING,
        ),
    )
    insert_run(
        conn,
        _baseline_run(
            id="r-succeeded",
            root_run_id="r-succeeded",
            status=RunStatus.SUCCEEDED,
        ),
    )

    results = list_pending_due(conn, now=_NOW + timedelta(minutes=5), limit=10)
    assert [r.id for r in results] == ["r-pending"]


def test_list_pending_due_excludes_future_due_at(tmp_path):
    """A pending run with ``due_at > now`` must NOT appear in
    the result — the wakeup function only handles due work."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(
        conn,
        _baseline_run(
            id="r-future",
            root_run_id="r-future",
            due_at=_NOW + timedelta(hours=1),
        ),
    )
    results = list_pending_due(conn, now=_NOW, limit=10)
    assert results == []


def test_list_pending_due_respects_limit(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    for i in range(5):
        rid = f"r-{i}"
        insert_run(
            conn,
            _baseline_run(
                id=rid,
                root_run_id=rid,
                due_at=_NOW - timedelta(seconds=i),
            ),
        )
    results = list_pending_due(conn, now=_NOW, limit=2)
    assert len(results) == 2


def test_list_pending_due_rejects_naive_now(tmp_path):
    conn = _migrate(tmp_path)
    naive = datetime(2026, 5, 15, 9, 0)
    with pytest.raises(NaiveDatetimeError, match="now"):
        list_pending_due(conn, now=naive, limit=10)


def test_list_pending_due_empty(tmp_path):
    conn = _migrate(tmp_path)
    assert list_pending_due(conn, now=_NOW, limit=10) == []


# ---------------------------------------------------------------------------
# Non-UTC datetimes (reviewer follow-up): lexical ISO compare in
# SQL only matches chronological order when every value is in
# the same offset. The storage layer normalises tz-aware
# datetimes to UTC before write + compare; pinning the
# regression so a future refactor that drops the .astimezone()
# call fails loudly.
# ---------------------------------------------------------------------------


def test_list_pending_due_non_utc_due_at_compares_chronologically(tmp_path):
    """A run with ``due_at = 10:00+03:00`` (i.e. ``07:00Z``)
    must be returned by ``list_pending_due(now=08:00Z)``.
    Without UTC normalisation, the stored ``10:00+03:00`` string
    would lexically sort AFTER ``08:00+00:00`` and the run
    would silently be missed."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)

    eastern_3 = datetime(
        2026, 5, 15, 10, 0, tzinfo=timezone(timedelta(hours=3))
    )
    insert_run(
        conn,
        _baseline_run(id="r-eastern", root_run_id="r-eastern", due_at=eastern_3),
    )

    cursor_utc = datetime(2026, 5, 15, 8, 0, tzinfo=timezone.utc)
    # 10:00+03:00 == 07:00Z, which IS <= 08:00Z.
    results = list_pending_due(conn, now=cursor_utc, limit=10)
    assert [r.id for r in results] == ["r-eastern"]


def test_list_pending_due_non_utc_now_compares_chronologically(tmp_path):
    """Mirror of the above: the ``now`` cursor itself in a
    non-UTC offset must still resolve correctly."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(
        conn,
        _baseline_run(
            id="r-utc",
            root_run_id="r-utc",
            due_at=datetime(2026, 5, 15, 7, 0, tzinfo=timezone.utc),
        ),
    )

    # 11:00+03:00 == 08:00Z, AFTER the run's 07:00Z due_at.
    cursor_eastern = datetime(
        2026, 5, 15, 11, 0, tzinfo=timezone(timedelta(hours=3))
    )
    results = list_pending_due(conn, now=cursor_eastern, limit=10)
    assert [r.id for r in results] == ["r-utc"]


def test_insert_run_normalises_non_utc_to_utc(tmp_path):
    """Round-trip pin: non-UTC due_at is stored normalised to
    ``+00:00`` so the stored bytes match between two equivalent
    moments expressed in different offsets."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)

    eastern = datetime(
        2026, 5, 15, 10, 0, tzinfo=timezone(timedelta(hours=3))
    )
    insert_run(
        conn,
        _baseline_run(id="r-tz", root_run_id="r-tz", due_at=eastern),
    )

    stored = conn.execute(
        "SELECT due_at FROM runs WHERE id = 'r-tz'"
    ).fetchone()[0]
    assert stored.endswith("+00:00")
    # And the round-tripped datetime equals the original moment
    # (Python datetime equality compares moments).
    fetched = get_run(conn, "r-tz")
    assert fetched.due_at == eastern


def test_mark_run_status_normalises_non_utc_extra_to_utc(tmp_path):
    """``mark_run_status`` extras containing non-UTC datetimes
    are stored normalised so the stored value matches what
    list_pending_due / SQL compares would see."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run(status=RunStatus.RUNNING))

    eastern = datetime(
        2026, 5, 15, 14, 0, tzinfo=timezone(timedelta(hours=3))
    )
    mark_run_status(
        conn,
        "run-abc",
        status=RunStatus.SUCCEEDED,
        completed_at=eastern,
    )
    stored = conn.execute(
        "SELECT completed_at FROM runs WHERE id = 'run-abc'"
    ).fetchone()[0]
    assert stored.endswith("+00:00")
    fetched = get_run(conn, "run-abc")
    assert fetched.completed_at == eastern


# ---------------------------------------------------------------------------
# limit guard (reviewer follow-up): ``LIMIT -1`` in SQLite
# means "no limit"; ``LIMIT 0`` returns nothing. Both are
# almost certainly caller bugs — the helper refuses both with
# ValueError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_list_pending_due_rejects_non_positive_limit(tmp_path, bad_limit):
    conn = _migrate(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        list_pending_due(conn, now=_NOW, limit=bad_limit)


def test_list_pending_due_accepts_limit_one(tmp_path):
    """1 is the lower edge of the allowed range — verify it
    works rather than being implicitly forbidden."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    for i in range(3):
        rid = f"r-{i}"
        insert_run(
            conn,
            _baseline_run(
                id=rid,
                root_run_id=rid,
                due_at=_NOW - timedelta(seconds=i),
            ),
        )
    results = list_pending_due(conn, now=_NOW, limit=1)
    assert len(results) == 1


# ===========================================================================
# list_runs_in_chain
# ===========================================================================


def test_list_runs_in_chain_orders_by_attempt(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    # First attempt: self-ref.
    insert_run(conn, _baseline_run(id="r-1", root_run_id="r-1", attempt=1))
    # Retries: same root_run_id, different ids, increasing attempt.
    insert_run(
        conn,
        _baseline_run(
            id="r-2",
            root_run_id="r-1",
            parent_run_id="r-1",
            attempt=2,
            fire_reason=FireReason.RETRY,
        ),
    )
    insert_run(
        conn,
        _baseline_run(
            id="r-3",
            root_run_id="r-1",
            parent_run_id="r-2",
            attempt=3,
            fire_reason=FireReason.RETRY,
        ),
    )

    chain = list_runs_in_chain(conn, "r-1")
    assert [r.id for r in chain] == ["r-1", "r-2", "r-3"]
    assert [r.attempt for r in chain] == [1, 2, 3]


def test_list_runs_in_chain_unknown_root_returns_empty(tmp_path):
    conn = _migrate(tmp_path)
    assert list_runs_in_chain(conn, "no-such-root") == []


def test_list_runs_in_chain_excludes_other_chains(tmp_path):
    """A different schedule's chain must NOT mix in."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "daily_audit")
    _seed_schedule(conn, "weekly_report")
    insert_run(
        conn,
        _baseline_run(id="x-1", root_run_id="x-1", schedule_id="daily_audit"),
    )
    insert_run(
        conn,
        _baseline_run(
            id="y-1",
            root_run_id="y-1",
            schedule_id="weekly_report",
        ),
    )
    chain = list_runs_in_chain(conn, "x-1")
    assert [r.id for r in chain] == ["x-1"]


# ===========================================================================
# mark_run_status
# ===========================================================================


def test_mark_run_status_flips_status(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run())
    mark_run_status(conn, "run-abc", status=RunStatus.RUNNING)
    fetched = get_run(conn, "run-abc")
    assert fetched.status is RunStatus.RUNNING


def test_mark_run_status_writes_extra_columns(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run(status=RunStatus.RUNNING))
    completed = _NOW + timedelta(minutes=5)
    mark_run_status(
        conn,
        "run-abc",
        status=RunStatus.SUCCEEDED,
        completed_at=completed,
        error=None,
    )
    fetched = get_run(conn, "run-abc")
    assert fetched.status is RunStatus.SUCCEEDED
    assert fetched.completed_at == completed
    assert fetched.error is None


def test_mark_run_status_writes_error_string(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run(status=RunStatus.RUNNING))
    mark_run_status(
        conn,
        "run-abc",
        status=RunStatus.FAILED,
        error="slack 503 from adapter",
    )
    fetched = get_run(conn, "run-abc")
    assert fetched.status is RunStatus.FAILED
    assert fetched.error == "slack 503 from adapter"


def test_mark_run_status_unpredicated_failed_to_cancelled(tmp_path):
    """Pin: helper accepts a contrived ``failed → cancelled``
    transition. The state machine in phase 4 would reject it;
    phase 3's storage layer doesn't enforce policy. This is the
    same pin as ``update_run_status_and_append_event``'s
    policy-freedom test."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run(status=RunStatus.FAILED))
    mark_run_status(conn, "run-abc", status=RunStatus.CANCELLED)
    fetched = get_run(conn, "run-abc")
    assert fetched.status is RunStatus.CANCELLED


def test_mark_run_status_missing_run_raises(tmp_path):
    conn = _migrate(tmp_path)
    with pytest.raises(RunNotFoundError, match="ghost"):
        mark_run_status(conn, "ghost", status=RunStatus.SUCCEEDED)


@pytest.mark.parametrize(
    "bad_key",
    [
        # ``status`` is a named keyword on the helper itself, so
        # Python rejects the call before the allowlist runs —
        # that's stronger than a runtime check. Not parametrised
        # here since the call site would raise TypeError, not
        # UnknownExtraColumnError.
        "schedule_id",
        "attempt",
        "root_run_id",
        "parent_run_id",
        "claimed_by",
        "claimed_at",
        "id",
        "fire_reason",
        "started_at = 'x'; DROP TABLE runs; --",
        "started_at, evil_col",
    ],
)
def test_mark_run_status_rejects_unknown_extra(tmp_path, bad_key):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run())
    with pytest.raises(UnknownExtraColumnError):
        mark_run_status(
            conn,
            "run-abc",
            status=RunStatus.SUCCEEDED,
            **{bad_key: "value"},
        )
    # Status unchanged.
    fetched = get_run(conn, "run-abc")
    assert fetched.status is RunStatus.PENDING


def test_mark_run_status_rejects_naive_datetime_extra(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    insert_run(conn, _baseline_run(status=RunStatus.RUNNING))
    naive = datetime(2026, 5, 15, 9, 5)
    with pytest.raises(NaiveDatetimeError, match="completed_at"):
        mark_run_status(
            conn,
            "run-abc",
            status=RunStatus.SUCCEEDED,
            completed_at=naive,
        )
    fetched = get_run(conn, "run-abc")
    assert fetched.status is RunStatus.RUNNING
    assert fetched.completed_at is None


# ===========================================================================
# Drift guard: shared allowlist with transactions module.
# ===========================================================================


def test_allowed_extra_columns_match_transactions_module():
    """Both ``mark_run_status`` and
    ``update_run_status_and_append_event`` write to the same
    table with the same neutral lifecycle columns. The two
    allowlists MUST stay in sync; this test fails loud if one
    drifts."""
    transactions_set = transactions_mod._ALLOWED_EXTRA_COLUMNS
    assert ALLOWED_EXTRA_RUN_COLUMNS == transactions_set


# ===========================================================================
# Connection guard
# ===========================================================================


def test_insert_run_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        insert_run(bare, _baseline_run())


def test_get_run_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        get_run(bare, "any")


def test_list_pending_due_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        list_pending_due(bare, now=_NOW, limit=10)


def test_list_runs_in_chain_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        list_runs_in_chain(bare, "any")


def test_mark_run_status_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        mark_run_status(bare, "any", status=RunStatus.SUCCEEDED)


# ===========================================================================
# Smoke checks — phase boundary
# ===========================================================================


def test_runs_module_has_no_io_imports():
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
    for _, member in vars(runs_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"runs module imports I/O libs: {sorted(leaked)}."
    )


def test_runs_module_has_no_dispatch_callables():
    """Slice-5 surface MUST NOT include claim_run, single-flight
    helpers, or any worker-shaped callable. Phase 4 owns those."""
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
        "release_claim",
        "recover_run",
    }
    for name, member in vars(runs_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"runs module exposes execution / claim-suggestive "
                f"callable: {name}"
            )
