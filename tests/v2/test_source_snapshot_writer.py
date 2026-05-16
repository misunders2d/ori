"""Phase 10 slice 4 — per-fire snapshot writer + retention.

Per ``docs/PHASE_10_PLAN.md`` §3.4 / §5. Verbatim .bin
(sha256(file)==content_hash every kind incl binary),
content-addressed dedup + dedup-shared-file safety,
COMMIT-before-unlink prune ordering, every on_oversize
branch.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.enums import OnOversizePolicy, SelectionMethod
from app.v2.migrations import runner
from app.v2.models.common import AuditPolicy
from app.v2.sources.contract import (
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import SourcePolicyError
from app.v2.sources.snapshot_writer import (
    prune_snapshots,
    write_snapshot,
)
from app.v2.storage.source_snapshots import get_snapshot


_NOW = datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path):
    path = tmp_path / "scheduler.db"
    init = sqlite3.connect(str(path))
    runner.apply_pending(init)
    init.close()

    def factory() -> sqlite3.Connection:
        c = sqlite3.connect(str(path))
        c.execute("PRAGMA foreign_keys=ON")
        return c

    return factory


def _seed_schedule(conn, schedule_id="sched_a"):
    conn.execute(
        "INSERT INTO schedules (id, owner, description, "
        "trigger_json, delivery_json, failure_json, "
        "audit_json, status, authored_at, hash) VALUES "
        "(?,?,?,?,?,?,?,?,?,?)",
        (
            schedule_id, "{}", "t", "{}", "{}", "{}", "{}",
            "active", _NOW.isoformat(), f"h-{schedule_id}",
        ),
    )


def _seed_run(conn, run_id, schedule_id="sched_a", *, at=_NOW):
    conn.execute(
        "INSERT INTO runs (id, schedule_id, fire_reason, "
        "due_at, status, attempt, root_run_id) VALUES "
        "(?,?,?,?,?,?,?)",
        (run_id, schedule_id, "scheduled", at.isoformat(),
         "pending", 1, run_id),
    )


def _result(*, source_id="in1", kind="text", content=None):
    if content is None:
        content = "stand-up at 9"
    cb = canonical_bytes(kind, content)
    return SourceResult(
        content=(
            {"_b64": "x"} if kind == "binary" else content
        ),
        content_bytes=cb,
        source_kind="source_literal",
        source_id=source_id,
        fetched_at=_NOW,
        content_hash=content_hash_for(cb),
        item_count=1,
        source_version=None,
        selection_method=SelectionMethod.CONTENT_HASH,
    )


def _audit(**kw) -> AuditPolicy:
    base = dict(
        keep_last_n_snapshots=30,
        dedup_by_content_hash=True,
        redact_fields=[],
        max_snapshot_bytes=1_000_000,
        on_oversize=OnOversizePolicy.FAIL_AND_ALERT,
    )
    base.update(kw)
    return AuditPolicy(**base)


# ===========================================================================
# Verbatim write + sha256(file)==content_hash for every kind
# ===========================================================================


@pytest.mark.parametrize(
    "kind,content",
    [
        ("text", "hello\n"),
        ("markdown", "# H\r\nbody"),
        ("yaml", "k: v\n"),
        ("json", {"b": 1, "a": [2, 3]}),
        ("dict", {"x": {"y": "z"}}),
        ("list", [1, "two"]),
        ("binary", bytes(range(48))),
    ],
)
def test_bin_holds_verbatim_bytes_and_rehashes(
    tmp_path, kind, content
):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        res = _result(kind=kind, content=content)
        out = write_snapshot(
            conn,
            result=res,
            schedule_id="sched_a",
            run_id="r1",
            audit=_audit(),
            repo_root=tmp_path,
        )
    finally:
        conn.close()

    abs_path = tmp_path / out.content_path
    on_disk = abs_path.read_bytes()
    assert on_disk == res.content_bytes  # VERBATIM, no envelope
    assert (
        "sha256:" + hashlib.sha256(on_disk).hexdigest()
        == res.content_hash
    )
    assert out.content_hash == res.content_hash
    assert out.deduped is False
    assert out.on_oversize_action is None

    conn = factory()
    try:
        row = get_snapshot(conn, run_id="r1", source_id="in1")
    finally:
        conn.close()
    assert row is not None
    assert row.content_path == out.content_path
    assert row.content_hash == res.content_hash


# ===========================================================================
# Content-addressed dedup + dedup-shared-file safety
# ===========================================================================


def test_dedup_reuses_file_and_prune_respects_shared_ref(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1", at=_NOW)
        _seed_run(conn, "r2", at=_NOW + timedelta(minutes=1))
        res1 = _result(content="same content")
        res2 = _result(content="same content")  # identical hash

        o1 = write_snapshot(
            conn, result=res1, schedule_id="sched_a",
            run_id="r1", audit=_audit(), repo_root=tmp_path,
        )
        o2 = write_snapshot(
            conn, result=res2, schedule_id="sched_a",
            run_id="r2", audit=_audit(), repo_root=tmp_path,
        )
    finally:
        conn.close()

    assert o1.deduped is False
    assert o2.deduped is True
    assert o2.content_path == o1.content_path
    # exactly ONE backing file for the two rows
    sources_dir = (tmp_path / o1.content_path).parent
    assert len(list(sources_dir.iterdir())) == 1

    # Prune the OLDER row (keep_last_n=1) → the shared file
    # MUST survive (still referenced by r2).
    conn = factory()
    try:
        pr = prune_snapshots(
            conn, factory, schedule_id="sched_a",
            source_id="in1", keep_last_n=1, repo_root=tmp_path,
        )
    finally:
        conn.close()
    assert pr.rows_deleted == 1
    assert pr.files_kept_shared == 1
    assert pr.files_unlinked == 0
    assert (tmp_path / o1.content_path).exists()  # shared, survives

    # Prune the LAST referencing row → file now unlinked.
    conn = factory()
    try:
        pr2 = prune_snapshots(
            conn, factory, schedule_id="sched_a",
            source_id="in1", keep_last_n=0, repo_root=tmp_path,
        )
    finally:
        conn.close()
    assert pr2.rows_deleted == 1
    assert pr2.files_unlinked == 1
    assert not (tmp_path / o1.content_path).exists()


@pytest.mark.parametrize(
    "prior_policy",
    [
        OnOversizePolicy.STORE_POINTER_ONLY,
        OnOversizePolicy.HASH_ONLY_NO_REPLAY,
    ],
)
def test_dedup_skips_bodyless_prior_row(tmp_path, prior_policy):
    """A prior store_pointer_only / hash_only_no_replay row
    has content_path "" — NO .bin exists for that hash. A
    later full snapshot of the same content MUST NOT dedup
    against it (that would skip the write and lose the body
    forever); it must materialise the .bin (codex slice-4
    🔴)."""
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1", at=_NOW)
        _seed_run(conn, "r2", at=_NOW + timedelta(minutes=1))
        _seed_run(conn, "r3", at=_NOW + timedelta(minutes=2))
        body = "x" * 200  # same content for all three

        # r1: bodyless prior row, same content_hash.
        o1 = write_snapshot(
            conn,
            result=_result(content=body),
            schedule_id="sched_a",
            run_id="r1",
            audit=_audit(max_snapshot_bytes=10, on_oversize=prior_policy),
            repo_root=tmp_path,
        )
        assert o1.content_path == ""

        # r2: full snapshot, same hash → must NOT dedup.
        o2 = write_snapshot(
            conn,
            result=_result(content=body),
            schedule_id="sched_a",
            run_id="r2",
            audit=_audit(),  # 1 MiB cap, dedup on
            repo_root=tmp_path,
        )
        # r3: full snapshot, same hash → NOW dedups against
        # the materialised r2 (valid dedup still works).
        o3 = write_snapshot(
            conn,
            result=_result(content=body),
            schedule_id="sched_a",
            run_id="r3",
            audit=_audit(),
            repo_root=tmp_path,
        )
    finally:
        conn.close()

    assert o1.content_hash == o2.content_hash == o3.content_hash
    assert o2.deduped is False
    assert o2.content_path != ""
    written = (tmp_path / o2.content_path).read_bytes()
    assert (
        "sha256:" + hashlib.sha256(written).hexdigest()
        == o2.content_hash
    )
    # valid dedup against the materialised row still holds
    assert o3.deduped is True
    assert o3.content_path == o2.content_path


# ===========================================================================
# Prune ordering — COMMIT before unlink; survivors intact
# ===========================================================================


def test_prune_commits_rows_before_unlink_survivors_intact(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        paths = []
        for i in range(5):
            rid = f"r{i}"
            _seed_run(conn, rid, at=_NOW + timedelta(minutes=i))
            out = write_snapshot(
                conn,
                result=_result(content=f"unique-{i}"),
                schedule_id="sched_a",
                run_id=rid,
                audit=_audit(),
                repo_root=tmp_path,
            )
            paths.append(out.content_path)
    finally:
        conn.close()

    conn = factory()
    try:
        pr = prune_snapshots(
            conn, factory, schedule_id="sched_a",
            source_id="in1", keep_last_n=2, repo_root=tmp_path,
        )
    finally:
        conn.close()

    assert pr.rows_deleted == 3
    assert pr.files_unlinked == 3

    # A FRESH conn sees the pruned rows GONE (committed
    # before any unlink) and the 2 survivors + their files
    # intact.
    fresh = factory()
    try:
        survivors = [
            get_snapshot(fresh, run_id=f"r{i}", source_id="in1")
            for i in range(5)
        ]
    finally:
        fresh.close()
    assert [s is not None for s in survivors] == [
        False, False, False, True, True
    ]
    # newest 2 (r3, r4) files intact; oldest 3 unlinked.
    assert not (tmp_path / paths[0]).exists()
    assert not (tmp_path / paths[2]).exists()
    assert (tmp_path / paths[3]).exists()
    assert (tmp_path / paths[4]).exists()


def test_keep_last_n_zero_prunes_all(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        out = write_snapshot(
            conn, result=_result(), schedule_id="sched_a",
            run_id="r1", audit=_audit(), repo_root=tmp_path,
        )
    finally:
        conn.close()
    conn = factory()
    try:
        pr = prune_snapshots(
            conn, factory, schedule_id="sched_a",
            source_id="in1", keep_last_n=0, repo_root=tmp_path,
        )
    finally:
        conn.close()
    assert pr.rows_deleted == 1
    assert pr.files_unlinked == 1
    assert not (tmp_path / out.content_path).exists()


def test_prune_orphan_unlink_failure_counted_not_raised(tmp_path):
    """A pruned content_path that cannot be unlinked (here:
    the path is a directory) is counted as an orphan and
    logged — never raised. Orphan ≠ data loss (the row is
    already gone)."""
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        out = write_snapshot(
            conn, result=_result(), schedule_id="sched_a",
            run_id="r1", audit=_audit(), repo_root=tmp_path,
        )
    finally:
        conn.close()
    # Replace the file with a directory so unlink() raises
    # OSError (IsADirectoryError).
    p = tmp_path / out.content_path
    p.unlink()
    p.mkdir()

    conn = factory()
    try:
        pr = prune_snapshots(
            conn, factory, schedule_id="sched_a",
            source_id="in1", keep_last_n=0, repo_root=tmp_path,
        )
    finally:
        conn.close()
    assert pr.rows_deleted == 1
    assert pr.orphans_logged == 1
    assert pr.files_unlinked == 0


# ===========================================================================
# on_oversize — every branch, explicit, no silent fallback
# ===========================================================================


def test_oversize_fail_and_alert_no_row_no_file(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        with pytest.raises(SourcePolicyError) as ei:
            write_snapshot(
                conn,
                result=_result(content="x" * 200),
                schedule_id="sched_a",
                run_id="r1",
                audit=_audit(
                    max_snapshot_bytes=10,
                    on_oversize=OnOversizePolicy.FAIL_AND_ALERT,
                ),
                repo_root=tmp_path,
            )
        assert ei.value.payload_code == "snapshot_oversize"
        assert ei.value.fallback_eligible is False
        assert get_snapshot(conn, run_id="r1", source_id="in1") is None
    finally:
        conn.close()
    assert not (tmp_path / "data").exists()


def test_oversize_store_pointer_only(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        out = write_snapshot(
            conn,
            result=_result(content="x" * 200),
            schedule_id="sched_a",
            run_id="r1",
            audit=_audit(
                max_snapshot_bytes=10,
                on_oversize=OnOversizePolicy.STORE_POINTER_ONLY,
            ),
            repo_root=tmp_path,
        )
        row = get_snapshot(conn, run_id="r1", source_id="in1")
    finally:
        conn.close()
    assert out.on_oversize_action == "store_pointer_only"
    assert out.content_path == ""
    assert row is not None and row.content_path == ""
    assert not (tmp_path / "data").exists()  # no body written


def test_oversize_hash_only_no_replay(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        res = _result(content="x" * 200)
        out = write_snapshot(
            conn,
            result=res,
            schedule_id="sched_a",
            run_id="r1",
            audit=_audit(
                max_snapshot_bytes=10,
                on_oversize=OnOversizePolicy.HASH_ONLY_NO_REPLAY,
            ),
            repo_root=tmp_path,
        )
        row = get_snapshot(conn, run_id="r1", source_id="in1")
    finally:
        conn.close()
    assert out.on_oversize_action == "hash_only_no_replay"
    assert out.content_path == ""
    assert out.content_hash == res.content_hash  # hash retained
    assert row is not None
    assert not (tmp_path / "data").exists()


def test_oversize_redact_and_store_brings_under_cap(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        big = {"keep": "ok", "secret": "S" * 500}
        res = _result(kind="json", content=big)
        out = write_snapshot(
            conn,
            result=res,
            schedule_id="sched_a",
            run_id="r1",
            audit=_audit(
                max_snapshot_bytes=60,
                redact_fields=["secret"],
                on_oversize=OnOversizePolicy.REDACT_AND_STORE,
            ),
            repo_root=tmp_path,
        )
        row = get_snapshot(conn, run_id="r1", source_id="in1")
    finally:
        conn.close()
    assert out.on_oversize_action == "redact_and_store"
    redacted_bytes = canonical_bytes("json", {"keep": "ok"})
    assert out.content_hash == content_hash_for(redacted_bytes)
    assert (tmp_path / out.content_path).read_bytes() == redacted_bytes
    assert row is not None and row.content_size == len(redacted_bytes)


def test_oversize_redact_still_too_big_fails(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
        big = {"keep": "K" * 500, "secret": "S" * 500}
        with pytest.raises(SourcePolicyError) as ei:
            write_snapshot(
                conn,
                result=_result(kind="json", content=big),
                schedule_id="sched_a",
                run_id="r1",
                audit=_audit(
                    max_snapshot_bytes=60,
                    redact_fields=["secret"],
                    on_oversize=OnOversizePolicy.REDACT_AND_STORE,
                ),
                repo_root=tmp_path,
            )
        assert (
            ei.value.payload_code == "snapshot_oversize_after_redact"
        )
        assert ei.value.fallback_eligible is False
    finally:
        conn.close()
