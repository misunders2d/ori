"""Phase 13 slice 2 — worker cross-fire-state SEAM.

Per ``docs/PHASE_13_PLAN.md`` §1 item 2 / §9 (Q1a) + the
claude-reviewer slice-2 hard-checks. Build-the-layer: the
seam is wired where a future DETERMINISTIC state-loader would
``state_read → pick → state_write`` (§6.5 / use-case 12) but
is DEAD — no stateful-flow loader/executor is shipped (Q1a),
so the live fire path never consults it.

- ``_cross_fire_state_seam`` reachable + STRICTLY delegates
  to the slice-1 ``state_read`` (NO CAS/policy re-declared).
- DEAD on the live path: an AST scan pins that
  ``_dispatch_emit_branch`` never CALLs it (a comment-mention
  is expected — the phase-11/12 seam-pin discipline).
- The phase-11 ``_fail_run`` reasoning boundary reason CODE
  ``reasoning_unsupported_pending_step_12`` is BYTE-IDENTICAL;
  phase-12 read-only-reasoning enforcement code is UNTOUCHED.

The authoritative BEHAVIOURAL regression pins for the live
boundaries —
``test_runtime_source_fire.py::test_reasoning_bearing_plan_fail_run_not_raise``
(phase-11) + the phase-12 ``test_validation_reasoning_tool_mode``
/ ``test_validation_customflow_friction`` suites — stay
UNMODIFIED and green (whole suite, 0 regression); not
duplicated here.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.v2.migrations import runner
from app.v2.runtime.state import StateView, state_read, state_write
from app.v2.runtime.worker import Worker

_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _worker() -> Worker:
    return Worker(
        conn_factory=lambda: sqlite3.connect(":memory:"),
        worker_id="w-state-seam",
        poll_interval=timedelta(seconds=10),
        clock=lambda: _NOW,
        run_id_factory=lambda: "nope",
        event_id_factory=lambda: "evt",
    )


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _seed_schedule(conn: sqlite3.Connection, sid: str = "daily_audit") -> None:
    conn.execute(
        "INSERT INTO schedules "
        "(id, owner, description, trigger_json, delivery_json, "
        " failure_json, audit_json, status, authored_at, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, "{}", "test", "{}", "{}", "{}", "{}", "active",
         _NOW.isoformat(), f"hash-{sid}"),
    )


# ---------------------------------------------------------------------------
# Reachable + strict slice-1 delegation
# ---------------------------------------------------------------------------


def test_seam_reachable_and_delegates_to_state_read(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    state_write(
        conn,
        schedule_id="daily_audit",
        key="cursor",
        value={"day": 7},
        written_by_run=None,
        now=_NOW,
        expected_version=None,
    )
    w = _worker()

    got = w._cross_fire_state_seam(
        conn, schedule_id="daily_audit", key="cursor"
    )

    assert isinstance(got, StateView)
    # STRICT delegation: identical to calling slice-1 directly.
    assert got == state_read(
        conn, schedule_id="daily_audit", key="cursor"
    )
    assert got.value == {"day": 7}
    assert got.version == 1


def test_seam_absent_key_is_none(tmp_path):
    conn = _migrate(tmp_path)
    _seed_schedule(conn)
    w = _worker()
    assert (
        w._cross_fire_state_seam(
            conn, schedule_id="daily_audit", key="absent"
        )
        is None
    )


# ---------------------------------------------------------------------------
# DEAD on the live path — AST pin (comment-mention is OK)
# ---------------------------------------------------------------------------


def test_seam_not_invoked_on_live_dispatch_path():
    src = textwrap.dedent(
        inspect.getsource(Worker._dispatch_emit_branch)
    )
    tree = ast.parse(src)
    called = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_cross_fire_state_seam"
        for n in ast.walk(tree)
    )
    assert not called, (
        "the cross-fire-state seam must remain DEAD on the "
        "live fire path — _dispatch_emit_branch must not CALL "
        "it (the seam comment naming it is expected)"
    )


# ---------------------------------------------------------------------------
# phase-11 / phase-12 boundaries byte-unchanged
# ---------------------------------------------------------------------------


def test_phase11_reason_code_byte_identical():
    src = inspect.getsource(Worker._dispatch_emit_branch)
    assert (
        'reason="reasoning_unsupported_pending_step_12"' in src
    ), "phase-11 reason CODE must stay byte-identical"


def test_phase12_enforcement_code_untouched():
    """Phase-12 read-only-reasoning enforcement code is
    UNTOUCHED by slice-2 — the rule fns + their distinct
    codes are still present in validation.py."""
    import app.v2.validation as v

    src = inspect.getsource(v)
    assert "def _validate_reasoning_tool_mode(" in src
    assert "def _validate_customflow_admin_friction(" in src
    assert '"reasoning_tool_write_in_read_only"' in src
    assert '"reasoning_tool_unresolved"' in src
    assert '"customflow_admin_approval_advisory"' in src
