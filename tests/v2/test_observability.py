"""Phase 15 slice 1 — observability pure-read primitives.

Per ``docs/PHASE_15_PLAN.md`` §1/§3/§5 + the claude-reviewer
round-1 disposition (Q1–Q7) + the slice-1 ``schedule_diff``
fork ruling (α). Five primitives:
``schedule_status`` / ``schedule_failures`` (per-schedule) /
``schedule_history`` / ``schedule_health`` (DI-clock) /
``registry_status`` (reuses phase-6 ``registry_cache``).

Pins: per-primitive correctness + a PURITY pin (events / runs
/ schedules rows byte-unchanged after every call — pure
read-only side-channel, ZERO mutation) + a folded
alias-robust AST import-hygiene pin for the new
``app/v2/observability/`` package (no module-load
``datetime.now`` / ``uuid4`` / vendor-SDK).

Reuses the shipped phase-7 lifecycle harness so the seed is
identical to the lifecycle tests.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import timedelta

import pytest

from app.v2.enums import EventKind
from app.v2.models.event import Event
from app.v2.observability import (
    failure_monitor_scan,
    registry_status,
    schedule_failures,
    schedule_health,
    schedule_history,
    schedule_status,
)
from app.v2.registry_cache.loader import save_cache
from app.v2.registry_cache.schemas import SlackChannelsCache
from app.v2.storage.events import append_event
from tests.v2.test_authoring_lifecycle_helper import (
    _UTC_NOW,
    _conn,
    _seed_run,
    _seed_schedule,
)


def _seed_runs(conn, *ids):
    """Insert valid run rows so run-scoped events satisfy the
    shipped append_event run↔schedule consistency guard (FK
    events.run_id → runs.id)."""
    for rid in ids:
        _seed_run(conn, schedule_id="sched_alpha", run_id=rid)


def _ev(conn, eid, *, kind, run_id=None, ts=None, correlates=None,
        payload=None):
    append_event(
        conn,
        Event(
            id=eid,
            run_id=run_id,
            schedule_id="sched_alpha",
            ts=ts or _UTC_NOW,
            kind=kind,
            payload=payload or {},
            correlates=correlates,
        ),
    )
    conn.commit()


def _fingerprint(conn):
    """Full ordered dump of every mutable table — equality
    pre/post a primitive call proves ZERO mutation."""
    out = {}
    for tbl in ("events", "runs", "schedules", "schedule_state"):
        out[tbl] = conn.execute(
            f"SELECT * FROM {tbl} ORDER BY 1"
        ).fetchall()
    return out


# ---------------------------------------------------------------------------
# schedule_status
# ---------------------------------------------------------------------------


def test_schedule_status_not_found(tmp_path):
    conn = _conn(tmp_path)
    r = schedule_status(conn, "missing")
    assert r.found is False
    assert r.status is None
    assert r.recent_runs == []
    conn.close()


def test_schedule_status_runs_and_unacked_alerts(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    _seed_runs(conn, "r1")
    t0 = _UTC_NOW
    _ev(conn, "e1", kind=EventKind.RUN_CREATED, run_id="r1", ts=t0)
    _ev(conn, "e2", kind=EventKind.RUN_STARTED, run_id="r1",
        ts=t0 + timedelta(seconds=1))
    _ev(conn, "e3", kind=EventKind.RUN_SUCCEEDED, run_id="r1",
        ts=t0 + timedelta(seconds=3))
    # An unacked admin alert + an acked one.
    _ev(conn, "a1", kind=EventKind.ADMIN_ALERT_SENT, run_id="r1")
    _ev(conn, "a2", kind=EventKind.ADMIN_ALERT_SENT, run_id="r1")
    _ev(conn, "a2ack", kind=EventKind.ADMIN_ALERT_ACKED,
        run_id="r1", correlates="a2")

    r = schedule_status(conn, "sched_alpha")
    assert r.found is True
    assert r.status == "active"
    assert len(r.recent_runs) == 1
    rs = r.recent_runs[0]
    assert rs.run_id == "r1"
    assert rs.status == "succeeded"
    assert rs.duration_ms == 2000  # 3s - 1s
    assert r.unacked_alert_count == 1  # a1 unacked, a2 acked
    conn.close()


# ---------------------------------------------------------------------------
# schedule_failures (per-schedule, merged, most-recent-first)
# ---------------------------------------------------------------------------


def test_schedule_failures_merges_and_orders(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    _seed_runs(conn, "r1", "r2", "r3", "r4")
    t0 = _UTC_NOW
    _ev(conn, "f1", kind=EventKind.RUN_FAILED, run_id="r1", ts=t0)
    _ev(conn, "f2", kind=EventKind.EMIT_FAILED, run_id="r2",
        ts=t0 + timedelta(seconds=2))
    _ev(conn, "f3", kind=EventKind.SOURCE_FAILED, run_id="r3",
        ts=t0 + timedelta(seconds=1))
    _ev(conn, "ok", kind=EventKind.RUN_SUCCEEDED, run_id="r4",
        ts=t0 + timedelta(seconds=5))

    r = schedule_failures(conn, "sched_alpha", limit=50)
    kinds = [f.kind for f in r.failures]
    assert "run_succeeded" not in kinds
    assert [f.event_id for f in r.failures] == ["f2", "f3", "f1"]
    conn.close()


def test_schedule_failures_limit_truncates_and_guards(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    _seed_runs(conn, *[f"r{i}" for i in range(5)])
    for i in range(5):
        _ev(conn, f"f{i}", kind=EventKind.RUN_FAILED, run_id=f"r{i}",
            ts=_UTC_NOW + timedelta(seconds=i))
    r = schedule_failures(conn, "sched_alpha", limit=3)
    assert len(r.failures) == 3
    assert [f.event_id for f in r.failures] == ["f4", "f3", "f2"]
    with pytest.raises(ValueError):
        schedule_failures(conn, "sched_alpha", limit=0)
    conn.close()


# ---------------------------------------------------------------------------
# schedule_history (chronological)
# ---------------------------------------------------------------------------


def test_schedule_history_chronological(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    _seed_runs(conn, "r1")
    t0 = _UTC_NOW
    _ev(conn, "h1", kind=EventKind.SCHEDULE_CREATED, ts=t0)
    _ev(conn, "h2", kind=EventKind.RUN_CREATED, run_id="r1",
        ts=t0 + timedelta(seconds=1))
    _ev(conn, "h3", kind=EventKind.RUN_SUCCEEDED, run_id="r1",
        ts=t0 + timedelta(seconds=2))
    r = schedule_history(conn, "sched_alpha")
    assert r.total == 3
    assert [e.event_id for e in r.timeline] == ["h1", "h2", "h3"]
    conn.close()


# ---------------------------------------------------------------------------
# schedule_health (DI-clock)
# ---------------------------------------------------------------------------


def test_schedule_health_rate_and_window(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    _seed_runs(conn, "r1", "r2", "r3", "r9")
    now = _UTC_NOW
    # In-window: 2 succeeded, 1 failed.
    _ev(conn, "s1", kind=EventKind.RUN_SUCCEEDED, run_id="r1",
        ts=now - timedelta(days=1))
    _ev(conn, "s2", kind=EventKind.RUN_SUCCEEDED, run_id="r2",
        ts=now - timedelta(days=2))
    _ev(conn, "x1", kind=EventKind.RUN_FAILED, run_id="r3",
        ts=now - timedelta(days=3))
    # Out-of-window (older than 7d) — must be excluded.
    _ev(conn, "old", kind=EventKind.RUN_FAILED, run_id="r9",
        ts=now - timedelta(days=30))
    r = schedule_health(conn, "sched_alpha", now=now)
    assert r.succeeded == 2
    assert r.failed == 1
    assert r.fire_ok_rate == pytest.approx(2 / 3)
    conn.close()


def test_schedule_health_none_when_no_terminal_in_window(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    r = schedule_health(conn, "sched_alpha", now=_UTC_NOW)
    assert r.succeeded == 0
    assert r.failed == 0
    assert r.fire_ok_rate is None  # explicit, never a silent 0.0
    conn.close()


# ---------------------------------------------------------------------------
# registry_status (reuses phase-6 registry_cache load_cache/is_stale)
# ---------------------------------------------------------------------------


def test_registry_status_absent_all_kinds(tmp_path):
    r = registry_status(now=_UTC_NOW, base=tmp_path)
    assert {c.kind for c in r.caches} == {
        "slack_channels",
        "google_sheets_items",
        "google_docs_items",
    }
    for c in r.caches:
        assert c.present is False
        assert c.fetched_at is None
        assert c.stale is None


def test_registry_status_present_fresh_and_stale(tmp_path):
    fresh = SlackChannelsCache(
        workspace_id="W1",
        fetched_at=_UTC_NOW,
        source="test",
        channels=[],
    )
    save_cache("slack_channels", fresh, base=tmp_path)
    # now == fetched_at → not stale (24h ttl).
    r = registry_status(now=_UTC_NOW, base=tmp_path)
    slack = next(c for c in r.caches if c.kind == "slack_channels")
    assert slack.present is True
    assert slack.stale is False
    # now far in the future → stale.
    r2 = registry_status(
        now=_UTC_NOW + timedelta(days=30), base=tmp_path
    )
    slack2 = next(
        c for c in r2.caches if c.kind == "slack_channels"
    )
    assert slack2.stale is True


# ---------------------------------------------------------------------------
# PURITY — every primitive leaves events/runs/schedules byte-unchanged
# ---------------------------------------------------------------------------


def test_all_primitives_are_pure_no_mutation(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    _seed_run(conn, schedule_id="sched_alpha", run_id="r1")
    _ev(conn, "e1", kind=EventKind.RUN_CREATED, run_id="r1")
    _ev(conn, "e2", kind=EventKind.RUN_FAILED, run_id="r1",
        ts=_UTC_NOW + timedelta(seconds=1))

    before = _fingerprint(conn)
    schedule_status(conn, "sched_alpha")
    schedule_failures(conn, "sched_alpha")
    schedule_history(conn, "sched_alpha")
    schedule_health(conn, "sched_alpha", now=_UTC_NOW)
    registry_status(now=_UTC_NOW, base=tmp_path)
    after = _fingerprint(conn)

    assert before == after  # ZERO mutation — pure side-channel
    conn.close()


# ---------------------------------------------------------------------------
# failure_monitor_scan — PURE detector (build-the-layer, NOT wired)
# ---------------------------------------------------------------------------


def _alert(conn, eid, *, kind, ts, correlates=None):
    """Schedule-level admin-alert event (run_id=None — skips
    the append_event run cross-check; admin alerts are
    schedule-scoped here)."""
    _ev(conn, eid, kind=kind, run_id=None, ts=ts,
        correlates=correlates)


def test_failure_monitor_returns_unacked_overdue(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    now = _UTC_NOW
    thr = timedelta(hours=1)
    # Two overdue (unacked, ts < now - 1h), oldest-first.
    _alert(conn, "s_old", kind=EventKind.ADMIN_ALERT_SENT,
           ts=now - timedelta(hours=5))
    _alert(conn, "s_mid", kind=EventKind.ADMIN_ALERT_SENT,
           ts=now - timedelta(hours=2))

    out = failure_monitor_scan(conn, now=now, threshold=thr)
    assert [a.alert_event_id for a in out] == ["s_old", "s_mid"]
    assert out[0].age_seconds == 5 * 3600
    assert all(a.schedule_id == "sched_alpha" for a in out)
    conn.close()


def test_failure_monitor_ack_clears_correlation(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    now = _UTC_NOW
    thr = timedelta(hours=1)
    _alert(conn, "s1", kind=EventKind.ADMIN_ALERT_SENT,
           ts=now - timedelta(hours=3))
    # An ack correlating s1 clears it even though it is overdue.
    _alert(conn, "ack1", kind=EventKind.ADMIN_ALERT_ACKED,
           ts=now - timedelta(hours=2), correlates="s1")

    out = failure_monitor_scan(conn, now=now, threshold=thr)
    assert out == []
    conn.close()


def test_failure_monitor_below_threshold_not_flagged(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    now = _UTC_NOW
    thr = timedelta(hours=1)
    # Unacked but only 30min old — NOT overdue.
    _alert(conn, "fresh", kind=EventKind.ADMIN_ALERT_SENT,
           ts=now - timedelta(minutes=30))
    out = failure_monitor_scan(conn, now=now, threshold=thr)
    assert out == []
    conn.close()


def test_failure_monitor_is_pure_no_mutation(tmp_path):
    conn = _conn(tmp_path)
    _seed_schedule(conn, schedule_id="sched_alpha")
    now = _UTC_NOW
    _alert(conn, "s1", kind=EventKind.ADMIN_ALERT_SENT,
           ts=now - timedelta(hours=3))
    _alert(conn, "ack1", kind=EventKind.ADMIN_ALERT_ACKED,
           ts=now - timedelta(hours=2), correlates="s1")
    _alert(conn, "s2", kind=EventKind.ADMIN_ALERT_SENT,
           ts=now - timedelta(hours=4))

    before = _fingerprint(conn)
    failure_monitor_scan(conn, now=now, threshold=timedelta(hours=1))
    after = _fingerprint(conn)
    assert before == after  # ZERO mutation — pure detector
    conn.close()


# ---------------------------------------------------------------------------
# Folded alias-robust AST import-hygiene pin (no new hygiene file —
# the slice-1 hard-check, mirrors the phase-14 idempotency fold)
# ---------------------------------------------------------------------------


def test_observability_pkg_no_module_load_nondeterminism():
    """No module-scope ``datetime.now`` / ``utcnow`` /
    ``uuid.uuid4`` / vendor-SDK call in any
    ``app/v2/observability/`` module. Alias-robust: scans the
    AST for the called-attribute NAME at module scope, so an
    ``import datetime as dt`` / ``from datetime import datetime``
    alias cannot smuggle a load-time clock past the grep the
    way a literal-token check would (the carried 11/12/13/14
    discipline)."""
    pkg = pathlib.Path("app/v2/observability")
    banned = {"now", "utcnow", "uuid4", "uuid1"}
    offenders: list[str] = []
    for py in sorted(pkg.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in tree.body:  # MODULE SCOPE only
            for sub in ast.walk(node):
                if isinstance(sub, ast.FunctionDef):
                    break
            if isinstance(node, (ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    nm = (
                        f.attr if isinstance(f, ast.Attribute)
                        else f.id if isinstance(f, ast.Name)
                        else None
                    )
                    if nm in banned:
                        offenders.append(f"{py.name}: {nm}()")
    assert offenders == [], (
        f"module-load nondeterminism: {offenders}"
    )
