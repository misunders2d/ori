"""Tests for ``app.v2.storage.source_snapshots``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.9:

- Insert + get round-trip preserves every field.
- Composite PK ``(run_id, source_id)`` enforces uniqueness.
- ``list_snapshots_by_hash`` returns matching rows ordered
  chronologically (with deterministic tie-break).

Plus:
- FK on ``run_id`` enforced — ghost run id raises.
- Naive ``fetched_at`` rejected; non-UTC normalised.
- ``get_snapshot`` returns None when absent.
- ``list_snapshots_by_hash`` empty when no match.
- assert_connection_ready called per helper.

Smoke:
- No I/O imports.
- No execution / claim-suggestive public callables.
- No in-place mutation helper (insert append-only;
  retention delete sanctioned in slice 4).
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import SelectionMethod
from app.v2.migrations import runner
from app.v2.models.snapshot import SourceSnapshotMetadata
from app.v2.storage import source_snapshots as snapshots_mod
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.serialization import NaiveDatetimeError
from app.v2.storage.source_snapshots import (
    content_path_referenced,
    delete_snapshot,
    get_snapshot,
    insert_snapshot,
    list_snapshots_by_hash,
    list_snapshots_for_schedule_source,
)


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(
    conn: sqlite3.Connection, schedule_id: str = "daily_audit"
) -> None:
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


def _baseline_meta(**overrides) -> SourceSnapshotMetadata:
    base = dict(
        run_id="run-abc",
        source_id="syllabus",
        content_hash=_HASH_A,
        content_path="data/sources/run-abc/syllabus.json",
        content_size=1234,
        fetched_at=_NOW,
        source_kind="source_drive_file",
        source_version="rev-12345",
        selection_method=SelectionMethod.STABLE_ID,
    )
    base.update(overrides)
    return SourceSnapshotMetadata(**base)


# ===========================================================================
# Insert + get round-trip
# ===========================================================================


def test_insert_and_get_round_trip(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    meta = _baseline_meta()
    pk = insert_snapshot(conn, meta)
    assert pk == ("run-abc", "syllabus")

    fetched = get_snapshot(
        conn, run_id="run-abc", source_id="syllabus"
    )
    assert fetched is not None
    assert fetched == meta


def test_round_trip_preserves_all_fields(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    meta = _baseline_meta(
        content_size=0,
        source_version=None,
        selection_method=SelectionMethod.CONTENT_HASH,
    )
    insert_snapshot(conn, meta)
    fetched = get_snapshot(
        conn, run_id=meta.run_id, source_id=meta.source_id
    )
    assert fetched.content_size == 0
    assert fetched.source_version is None
    assert fetched.selection_method is SelectionMethod.CONTENT_HASH


@pytest.mark.parametrize(
    "method",
    [
        SelectionMethod.STABLE_ID,
        SelectionMethod.CONTENT_HASH,
        SelectionMethod.ROW_NUMBER,
    ],
)
def test_round_trip_preserves_each_selection_method(tmp_path, method):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    meta = _baseline_meta(selection_method=method)
    insert_snapshot(conn, meta)
    fetched = get_snapshot(
        conn, run_id=meta.run_id, source_id=meta.source_id
    )
    assert fetched.selection_method is method


# ===========================================================================
# Composite PK + FK
# ===========================================================================


def test_duplicate_composite_pk_raises(tmp_path):
    """``(run_id, source_id)`` is the composite PK. A second
    insert with the same pair fails."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    insert_snapshot(conn, _baseline_meta())
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(conn, _baseline_meta())


def test_distinct_source_ids_under_same_run_allowed(tmp_path):
    """Same run, different source_id → fine (the run pulled two
    sources)."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    insert_snapshot(conn, _baseline_meta(source_id="syllabus"))
    insert_snapshot(conn, _baseline_meta(source_id="bq_data"))

    assert get_snapshot(
        conn, run_id="run-abc", source_id="syllabus"
    ) is not None
    assert get_snapshot(
        conn, run_id="run-abc", source_id="bq_data"
    ) is not None


def test_distinct_runs_with_same_source_id_allowed(tmp_path):
    """Two different runs each pulled the same source_id (e.g.
    daily ``syllabus``). Composite PK still distinct."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="r1")
    _seed_run(conn, run_id="r2")
    insert_snapshot(
        conn, _baseline_meta(run_id="r1", source_id="syllabus")
    )
    insert_snapshot(
        conn, _baseline_meta(run_id="r2", source_id="syllabus")
    )
    assert (
        get_snapshot(conn, run_id="r1", source_id="syllabus") is not None
    )
    assert (
        get_snapshot(conn, run_id="r2", source_id="syllabus") is not None
    )


def test_missing_run_fk_raises(tmp_path):
    """FK on ``source_snapshots.run_id``. Ghost run → FK fires."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    # No run inserted.
    with pytest.raises(sqlite3.IntegrityError):
        insert_snapshot(conn, _baseline_meta(run_id="ghost-run"))


# ===========================================================================
# get_snapshot edge cases
# ===========================================================================


def test_get_missing_returns_none(tmp_path):
    conn = _migrate(tmp_path)
    assert (
        get_snapshot(conn, run_id="any", source_id="any") is None
    )


# ===========================================================================
# list_snapshots_by_hash
# ===========================================================================


def test_list_by_hash_returns_all_matching(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="r1")
    _seed_run(conn, run_id="r2")
    _seed_run(conn, run_id="r3")
    insert_snapshot(
        conn, _baseline_meta(run_id="r1", content_hash=_HASH_A)
    )
    insert_snapshot(
        conn,
        _baseline_meta(
            run_id="r2", content_hash=_HASH_A, source_id="alt"
        ),
    )
    insert_snapshot(
        conn,
        _baseline_meta(run_id="r3", content_hash=_HASH_B),
    )

    matches = list_snapshots_by_hash(conn, _HASH_A)
    assert len(matches) == 2
    assert all(m.content_hash == _HASH_A for m in matches)


def test_list_by_hash_orders_chronologically(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="r1")
    _seed_run(conn, run_id="r2")
    _seed_run(conn, run_id="r3")
    insert_snapshot(
        conn,
        _baseline_meta(
            run_id="r2",
            content_hash=_HASH_A,
            fetched_at=_NOW + timedelta(seconds=20),
        ),
    )
    insert_snapshot(
        conn,
        _baseline_meta(
            run_id="r1",
            content_hash=_HASH_A,
            fetched_at=_NOW,
        ),
    )
    insert_snapshot(
        conn,
        _baseline_meta(
            run_id="r3",
            content_hash=_HASH_A,
            fetched_at=_NOW + timedelta(seconds=10),
        ),
    )

    matches = list_snapshots_by_hash(conn, _HASH_A)
    assert [m.run_id for m in matches] == ["r1", "r3", "r2"]


def test_list_by_hash_tie_break_deterministic(tmp_path):
    """Two snapshots with identical fetched_at must come back in
    a deterministic order (secondary sort on run_id, then
    source_id)."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, run_id="r2")
    _seed_run(conn, run_id="r1")
    insert_snapshot(
        conn,
        _baseline_meta(run_id="r2", content_hash=_HASH_A, fetched_at=_NOW),
    )
    insert_snapshot(
        conn,
        _baseline_meta(run_id="r1", content_hash=_HASH_A, fetched_at=_NOW),
    )
    matches = list_snapshots_by_hash(conn, _HASH_A)
    assert [m.run_id for m in matches] == ["r1", "r2"]


def test_list_by_hash_empty_when_no_match(tmp_path):
    conn = _migrate(tmp_path)
    assert list_snapshots_by_hash(conn, _HASH_A) == []


# ===========================================================================
# Datetime + UTC normalisation
# ===========================================================================


def test_insert_rejects_naive_fetched_at(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    naive = datetime(2026, 5, 15, 9, 0)
    naive_meta = SourceSnapshotMetadata.model_construct(
        run_id="run-abc",
        source_id="syllabus",
        content_hash=_HASH_A,
        content_path="data/sources/x.json",
        content_size=10,
        fetched_at=naive,
        source_kind="source_drive_file",
        source_version=None,
        selection_method=SelectionMethod.STABLE_ID,
    )
    with pytest.raises(NaiveDatetimeError, match="fetched_at"):
        insert_snapshot(conn, naive_meta)
    assert get_snapshot(
        conn, run_id="run-abc", source_id="syllabus"
    ) is None


def test_insert_normalises_non_utc_fetched_at(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn)
    eastern = datetime(
        2026, 5, 15, 12, 0, tzinfo=timezone(timedelta(hours=3))
    )
    meta = _baseline_meta(fetched_at=eastern)
    insert_snapshot(conn, meta)
    stored = conn.execute(
        "SELECT fetched_at FROM source_snapshots WHERE run_id = 'run-abc'"
    ).fetchone()[0]
    assert stored.endswith("+00:00")
    fetched = get_snapshot(
        conn, run_id="run-abc", source_id="syllabus"
    )
    assert fetched.fetched_at == eastern


# ===========================================================================
# Connection guard
# ===========================================================================


def test_insert_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        insert_snapshot(bare, _baseline_meta())


def test_get_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        get_snapshot(bare, run_id="x", source_id="y")


def test_list_by_hash_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        list_snapshots_by_hash(bare, _HASH_A)


# ===========================================================================
# Smoke
# ===========================================================================


def test_module_exposes_no_inplace_mutation_helper():
    """``insert_snapshot`` stays append-only + content-
    addressed. Phase-10 slice 4 sanctions ONE delete —
    retention (``delete_snapshot``, design §5.3.5 / plan
    §3.4); in-place MUTATION of an existing row
    (update/modify/overwrite) remains forbidden (a snapshot
    is immutable history — a change is a new row, removal
    is retention only)."""
    public = {
        name for name in dir(snapshots_mod) if not name.startswith("_")
    }
    forbidden = {
        "update_snapshot",
        "modify_snapshot",
        "overwrite_snapshot",
        "drop_snapshot",
        "truncate_snapshots",
    }
    leaked = public & forbidden
    assert not leaked, (
        f"source_snapshots module exposes in-place mutation "
        f"helpers: {sorted(leaked)}. A snapshot is immutable "
        "history; a change is a NEW row, removal is "
        "retention-only (delete_snapshot)."
    )
    # The sanctioned retention delete primitive IS present
    # (slice 4).
    assert "delete_snapshot" in public
    assert "list_snapshots_for_schedule_source" in public
    assert "content_path_referenced" in public


def test_source_snapshots_module_has_no_io_imports():
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
    for _, member in vars(snapshots_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"source_snapshots module imports I/O libs: {sorted(leaked)}."
    )


def test_source_snapshots_module_has_no_dispatch_callables():
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
    for name, member in vars(snapshots_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"source_snapshots module exposes execution / claim "
                f"suggestive callable: {name}"
            )


# ===========================================================================
# Phase-10 slice 4 — retention primitives (the sole
# sanctioned delete surface; plan §3.4)
# ===========================================================================


def _seed_run_for(
    conn: sqlite3.Connection,
    run_id: str,
    schedule_id: str,
    *,
    at: datetime = _NOW,
) -> None:
    conn.execute(
        "INSERT INTO runs (id, schedule_id, fire_reason, "
        "due_at, status, attempt, root_run_id) VALUES "
        "(?,?,?,?,?,?,?)",
        (run_id, schedule_id, "scheduled", at.isoformat(),
         "pending", 1, run_id),
    )


def test_list_for_schedule_source_newest_first_and_scoped(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn, "sched_a")
    _seed_schedule(conn, "sched_b")
    _seed_run_for(conn, "ra1", "sched_a", at=_NOW)
    _seed_run_for(
        conn, "ra2", "sched_a", at=_NOW + timedelta(minutes=5)
    )
    _seed_run_for(conn, "rb1", "sched_b", at=_NOW)
    insert_snapshot(
        conn,
        _baseline_meta(run_id="ra1", source_id="s", fetched_at=_NOW),
    )
    insert_snapshot(
        conn,
        _baseline_meta(
            run_id="ra2",
            source_id="s",
            fetched_at=_NOW + timedelta(minutes=5),
        ),
    )
    insert_snapshot(
        conn,
        _baseline_meta(run_id="rb1", source_id="s", fetched_at=_NOW),
    )

    got = list_snapshots_for_schedule_source(
        conn, schedule_id="sched_a", source_id="s"
    )
    # newest first; sched_b excluded.
    assert [r[0] for r in got] == ["ra2", "ra1"]
    assert all(r[1] == "s" for r in got)


def test_delete_snapshot_rowcount_idempotent(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, "run-abc")
    insert_snapshot(conn, _baseline_meta(run_id="run-abc"))

    assert delete_snapshot(conn, run_id="run-abc", source_id="syllabus") == 1
    assert delete_snapshot(conn, run_id="run-abc", source_id="syllabus") == 0
    assert get_snapshot(conn, run_id="run-abc", source_id="syllabus") is None


def test_content_path_referenced(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _seed_run(conn, "run-abc")
    insert_snapshot(
        conn,
        _baseline_meta(
            run_id="run-abc", content_path="data/x/p.bin"
        ),
    )
    assert content_path_referenced(conn, "data/x/p.bin") is True
    assert content_path_referenced(conn, "data/x/other.bin") is False
    delete_snapshot(conn, run_id="run-abc", source_id="syllabus")
    assert content_path_referenced(conn, "data/x/p.bin") is False
