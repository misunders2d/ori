"""Phase 16 slice 2 — GATED idempotent migrate + lineage backfill.

Per ``docs/PHASE_16_PLAN.md`` §1/§4/§9 + the claude-reviewer
round-1 disposition + the slice-2 backfill fork ruling (α):
backfill = exactly ONE shipped ``MIGRATION_V1_TO_V2_COMPLETE``
lineage event per migrated schedule (NO per-fire replay, NO
v1-audit parser).

Pins: dry-run DEFAULT is PURE (ZERO write) + gated confirm
migrates ATOMICALLY (schedule + schedule_created + lineage,
both-or-neither) + idempotent re-run (no dup schedule, no dup
lineage) + the in-TX both-or-neither rollback + honest skip
paths. The slice-1 package AST pin auto-covers
``commit.py`` (v1 READ-ONLY import-confinement + no
module-load nondeterminism).
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from app.contracts.schema import (
    Contract,
    CronTrigger,
    EmitStep,
    OnDemandTrigger,
)
from app.v2.migration import (
    MigrationBinding,
    MigrationOutcome,
    migrate_contracts,
)
from tests.v2.test_authoring_lifecycle_helper import _UTC_NOW, _conn


def _contract(**over) -> Contract:
    base = dict(
        id="daily_digest",
        description="Daily Amazon sales digest summary.",
        author="U_AUTHOR",
        trigger=CronTrigger(cron="0 9 * * *", timezone="UTC"),
        emit=[EmitStep(adapter="slack_post", args={"channel": "C1"})],
    )
    base.update(over)
    return Contract(**base)


class _StubStore:
    def __init__(self, contracts):
        self._c = contracts

    def list_all(self):
        return sorted(self._c)

    def load_latest(self, cid):
        return self._c[cid]


def _eid_factory():
    n = {"i": 0}

    def _next():
        n["i"] += 1
        return f"evt-{n['i']:08d}-1111-1111-1111-111111111111"

    return _next


def _clock():
    return _UTC_NOW


_BINDING = MigrationBinding(
    platform="slack", target_session_id="C012ABCDE"
)


def _fingerprint(conn):
    out = {}
    for t in ("events", "runs", "schedules", "schedule_state"):
        out[t] = conn.execute(
            f"SELECT * FROM {t} ORDER BY 1"
        ).fetchall()
    return out


def _events(conn, schedule_id):
    return conn.execute(
        "SELECT kind, payload_json FROM events "
        "WHERE schedule_id = ? ORDER BY ts ASC",
        (schedule_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Dry-run DEFAULT is PURE
# ---------------------------------------------------------------------------


def test_dry_run_default_is_pure_no_write(tmp_path):
    conn = _conn(tmp_path)
    store = _StubStore({"daily_digest": _contract()})
    before = _fingerprint(conn)
    report = migrate_contracts(
        store=store,
        conn=conn,
        bindings={"daily_digest": _BINDING},
        event_id_factory=_eid_factory(),
        clock=_clock,
    )  # confirm defaults False
    after = _fingerprint(conn)
    assert before == after  # ZERO write — dry-run is PURE
    assert report.dry_run is True
    assert report.migrated_count == 0
    assert report.entries[0].outcome is (
        MigrationOutcome.DRY_RUN_WOULD_MIGRATE
    )
    conn.close()


# ---------------------------------------------------------------------------
# Gated confirm — atomic schedule + schedule_created + lineage
# ---------------------------------------------------------------------------


def test_confirm_migrates_atomic_with_lineage(tmp_path):
    conn = _conn(tmp_path)
    store = _StubStore({"daily_digest": _contract()})
    report = migrate_contracts(
        store=store,
        conn=conn,
        bindings={"daily_digest": _BINDING},
        event_id_factory=_eid_factory(),
        clock=_clock,
        confirm=True,
    )
    assert report.dry_run is False
    assert report.migrated_count == 1
    e = report.entries[0]
    assert e.outcome is MigrationOutcome.MIGRATED
    assert e.schedule_id == "daily_digest"

    # v2 schedule row present.
    row = conn.execute(
        "SELECT id FROM schedules WHERE id = ?", ("daily_digest",)
    ).fetchone()
    assert row is not None

    kinds = [k for k, _ in _events(conn, "daily_digest")]
    assert "schedule_created" in kinds
    assert "migration_v1_to_v2_complete" in kinds
    # Lineage is honestly provenance-marked + distinguishable
    # from a live fire AND from schedule_created (§13).
    lineage = next(
        json.loads(p)
        for k, p in _events(conn, "daily_digest")
        if k == "migration_v1_to_v2_complete"
    )
    assert lineage["backfill"] is True
    assert lineage["migrated_from"].startswith("daily_digest@")
    assert "run_succeeded" not in kinds  # NOT a live fire
    conn.close()


# ---------------------------------------------------------------------------
# Idempotent re-run — no dup schedule, no dup lineage
# ---------------------------------------------------------------------------


def test_idempotent_rerun(tmp_path):
    conn = _conn(tmp_path)
    store = _StubStore({"daily_digest": _contract()})
    kw = dict(
        store=store,
        conn=conn,
        bindings={"daily_digest": _BINDING},
        clock=_clock,
    )
    r1 = migrate_contracts(
        event_id_factory=_eid_factory(), confirm=True, **kw
    )
    assert r1.migrated_count == 1
    r2 = migrate_contracts(
        event_id_factory=_eid_factory(), confirm=True, **kw
    )
    assert r2.migrated_count == 0
    assert r2.entries[0].outcome is (
        MigrationOutcome.SKIPPED_ALREADY_EXISTS
    )
    # Exactly ONE schedule + ONE lineage event.
    assert conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE id = ?",
        ("daily_digest",),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE schedule_id = ? "
        "AND kind = 'migration_v1_to_v2_complete'",
        ("daily_digest",),
    ).fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# In-TX both-or-neither rollback
# ---------------------------------------------------------------------------


def test_atomic_both_or_neither_rollback(tmp_path):
    conn = _conn(tmp_path)
    store = _StubStore({"daily_digest": _contract()})

    # A constant event-id factory ⇒ schedule_created.id ==
    # lineage.id ⇒ the SECOND append_event raises IntegrityError
    # (events.id PRIMARY KEY) mid-transaction ⇒ the WHOLE TX
    # rolls back: NO schedule, NO events (both-or-neither).
    def _constant():
        return "evt-dup-0000-1111-1111-111111111111"

    report = migrate_contracts(
        store=store,
        conn=conn,
        bindings={"daily_digest": _BINDING},
        event_id_factory=_constant,
        clock=_clock,
        confirm=True,
    )
    assert report.entries[0].outcome is (
        MigrationOutcome.SKIPPED_ALREADY_EXISTS  # IntegrityError path
    )
    # NEITHER persisted.
    assert conn.execute(
        "SELECT COUNT(*) FROM schedules WHERE id = ?",
        ("daily_digest",),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE schedule_id = ?",
        ("daily_digest",),
    ).fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Honest skip paths
# ---------------------------------------------------------------------------


def test_skipped_not_migratable_no_write(tmp_path):
    conn = _conn(tmp_path)
    store = _StubStore(
        {"od": _contract(id="od", trigger=OnDemandTrigger())}
    )
    before = _fingerprint(conn)
    report = migrate_contracts(
        store=store,
        conn=conn,
        bindings={"od": _BINDING},
        event_id_factory=_eid_factory(),
        clock=_clock,
        confirm=True,
    )
    assert _fingerprint(conn) == before  # ZERO write
    assert report.entries[0].outcome is (
        MigrationOutcome.SKIPPED_NOT_MIGRATABLE
    )
    assert report.entries[0].skip_reasons
    conn.close()


def test_skipped_no_binding_no_write(tmp_path):
    conn = _conn(tmp_path)
    store = _StubStore({"daily_digest": _contract()})
    before = _fingerprint(conn)
    report = migrate_contracts(
        store=store,
        conn=conn,
        bindings={},  # operator supplied none
        event_id_factory=_eid_factory(),
        clock=_clock,
        confirm=True,
    )
    assert _fingerprint(conn) == before
    assert report.entries[0].outcome is (
        MigrationOutcome.SKIPPED_NO_BINDING
    )
    conn.close()


# ---------------------------------------------------------------------------
# commit.py composes ONLY shipped storage primitives (no bespoke SQL)
# ---------------------------------------------------------------------------


def test_commit_no_bespoke_sql():
    """The migrate write composes the shipped
    transaction/insert_schedule/append_event primitives — it
    must NOT hand-roll an INSERT/UPDATE/DELETE
    conn.execute(...) (no SQL reimpl, the Q discipline)."""
    src = pathlib.Path(
        "app/v2/migration/commit.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                sql = str(node.args[0].value).upper()
                assert not any(
                    w in sql
                    for w in ("INSERT", "UPDATE", "DELETE")
                ), f"bespoke write SQL in commit.py: {sql!r}"
