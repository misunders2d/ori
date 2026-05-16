"""Phase 14 slice 1 — pure `prior_emit_succeeded` dedup READ.

Per ``docs/PHASE_14_PLAN.md`` §1 / §3 / §9 (Q1/Q6) + the
claude-reviewer slice-1 hard-checks:

- Pure DI-conn READ; NO SQL reimpl (delegates to the shipped
  ``storage/events.py::get_last_emit_succeeded``); NO mutation.
- Matches ``emit_succeeded`` KIND **AND** payload-carries-
  THIS-`idempotency_key` — collision-safe: a distinct key, or
  the same key on a non-`emit_succeeded` kind, NEVER matches.
- Reuses ``compute_idempotency_key`` — no key-derivation
  reimpl.
- Folded alias-robust import-hygiene assertion for
  ``app.v2.idempotency`` (no new module ⇒ folded here, not a
  new phase-14 hygiene file — per the slice-1 hard-check).
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import app.v2.idempotency as idem_mod
from app.v2.idempotency import (
    compute_idempotency_key,
    prior_emit_succeeded,
)
from app.v2.enums import EventKind
from app.v2.migrations import runner
from app.v2.models.event import Event
from app.v2.storage.events import append_event

_NOW = datetime(2026, 5, 16, 9, 0, tzinfo=timezone.utc)


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(conn: sqlite3.Connection, sid: str = "sched_a") -> None:
    conn.execute(
        "INSERT INTO schedules "
        "(id, owner, description, trigger_json, delivery_json, "
        " failure_json, audit_json, status, authored_at, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, "{}", "test", "{}", "{}", "{}", "{}", "active",
         _NOW.isoformat(), f"hash-{sid}"),
    )


_EID = {"i": 0}


def _evt_id() -> str:
    _EID["i"] += 1
    return f"evt-{_EID['i']:08d}-1111-1111-1111-111111111111"


def _append(
    conn,
    *,
    sid="sched_a",
    kind=EventKind.EMIT_SUCCEEDED,
    key="sched_a:root1:post",
):
    append_event(
        conn,
        Event(
            id=_evt_id(),
            run_id=None,
            schedule_id=sid,
            ts=_NOW,
            kind=kind,
            payload={"worker_id": "w", "idempotency_key": key},
        ),
    )


_K = compute_idempotency_key(
    schedule_id="sched_a", root_run_id="root1", emit_id="post"
)


# ---------------------------------------------------------------------------
# True iff a prior emit_succeeded with the EXACT key
# ---------------------------------------------------------------------------


def test_false_when_ledger_empty(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    assert prior_emit_succeeded(conn, idempotency_key=_K) is False


def test_true_after_emit_succeeded_with_key(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _append(conn, kind=EventKind.EMIT_SUCCEEDED, key=_K)
    assert prior_emit_succeeded(conn, idempotency_key=_K) is True


def test_distinct_key_never_matches(tmp_path):
    """A different (schedule/root_run/emit_id) ⇒ a different
    compute_idempotency_key ⇒ no false match."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _append(conn, kind=EventKind.EMIT_SUCCEEDED, key=_K)
    other = compute_idempotency_key(
        schedule_id="sched_a", root_run_id="root2", emit_id="post"
    )
    assert other != _K
    assert prior_emit_succeeded(conn, idempotency_key=other) is False


# ---------------------------------------------------------------------------
# Collision-safe: kind filter is load-bearing
# ---------------------------------------------------------------------------


def test_emit_failed_with_same_key_does_not_match(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _append(conn, kind=EventKind.EMIT_FAILED, key=_K)
    assert prior_emit_succeeded(conn, idempotency_key=_K) is False


def test_run_succeeded_with_same_key_does_not_match(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _append(conn, kind=EventKind.RUN_SUCCEEDED, key=_K)
    assert prior_emit_succeeded(conn, idempotency_key=_K) is False


def test_emit_succeeded_with_different_key_does_not_match(tmp_path):
    """NOT any emit_succeeded — only one carrying THIS key."""
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _append(
        conn,
        kind=EventKind.EMIT_SUCCEEDED,
        key="sched_a:rootX:other_emit",
    )
    assert prior_emit_succeeded(conn, idempotency_key=_K) is False


# ---------------------------------------------------------------------------
# Pure — no mutation
# ---------------------------------------------------------------------------


def test_pure_no_mutation(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    _append(conn, kind=EventKind.EMIT_SUCCEEDED, key=_K)

    def _count():
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    before = _count()
    r1 = prior_emit_succeeded(conn, idempotency_key=_K)
    r2 = prior_emit_succeeded(conn, idempotency_key=_K)
    assert r1 is True and r2 is True
    assert _count() == before  # read-only, no rows written


# ---------------------------------------------------------------------------
# Folded alias-robust import hygiene (no new module ⇒ folded here)
# ---------------------------------------------------------------------------


_FORBIDDEN_LOAD = {
    "app.v2.runtime._defaults", "slack_sdk", "google",
    "googleapiclient", "oauth2client", "httpx", "requests",
    "urllib3", "aiohttp",
}
_FORBIDDEN_CALL = {"uuid.uuid4", "datetime.now", "datetime.datetime.now"}


def _alias_map(tree):
    m = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                m[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                m[a.asname or a.name] = f"{n.module}.{a.name}"
    return m


def test_idempotency_module_hygiene_clean():
    """app.v2.idempotency (extended in slice 1) carries no
    forbidden module-load import and no uuid.uuid4 /
    datetime.now call (alias-robust — the phase-11/12/13
    detector, folded here since no new module appeared)."""
    src = inspect.getsource(idem_mod)
    tree = ast.parse(src)
    amap = _alias_map(tree)

    # Module-scope imports: skip into func/class bodies.
    class _Top(ast.NodeVisitor):
        def __init__(self):
            self.names = set()

        def visit_FunctionDef(self, n):  # noqa: N802
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, n):  # noqa: N802
            return

        def visit_Import(self, n):  # noqa: N802
            for a in n.names:
                self.names.add(a.name)

        def visit_ImportFrom(self, n):  # noqa: N802
            self.names.add(n.module or "")

    top = _Top()
    top.visit(tree)
    leaked = [
        i for i in top.names
        if any(i == b or i.startswith(b + ".") for b in _FORBIDDEN_LOAD)
    ]
    assert not leaked, f"forbidden module-load import: {leaked!r}"

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        chain = []
        f = n.func
        while isinstance(f, ast.Attribute):
            chain.insert(0, f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            chain.insert(0, f.id)
        if not chain:
            continue
        root = chain[0]
        canon = (
            amap[root] + ("." + ".".join(chain[1:]) if chain[1:] else "")
            if root in amap
            else ".".join(chain)
        )
        assert canon not in _FORBIDDEN_CALL, (
            f"forbidden call {canon} in app.v2.idempotency"
        )
