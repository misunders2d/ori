"""Tests for ``app.v2.storage.execution_plans``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.5:

- Insert + get round-trip preserves every sub-model (InputSpec,
  ReasoningStep, EmitStep, Acceptance, FailureAction,
  enforcement enum).
- Duplicate hash raises ``IntegrityError``.
- Plans are immutable: the module exposes no update_* /
  delete_* helpers.
- ``ExecutionPlanNotFrozenError`` on empty hash.
- ``get_execution_plan`` returns ``None`` for unknown hash.
- enforcement CHECK rejects unknown values (DDL-level guard
  exercised via raw SQL).
- ``assert_connection_ready`` runs on each helper.

Smoke:
- Module imports no I/O libs.
- No execution / claim-suggestive public callables.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from app.v2.enums import EnforcementMode, FailureActionType, ToolMode
from app.v2.migrations import runner
from app.v2.models.execution_plan import (
    Acceptance,
    EmitStep,
    ExecutionPlan,
    FailureAction,
    Gate,
    InputSpec,
    OutputSpec,
    ReasoningStep,
    Retry,
)
from app.v2.storage import execution_plans as plans_mod
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.execution_plans import (
    ExecutionPlanNotFrozenError,
    get_execution_plan,
    insert_execution_plan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _baseline_plan(**overrides) -> ExecutionPlan:
    base = dict(
        id="daily_audit_plan",
        description="walk the audit + post a summary",
        author="sergey@mellanni.com",
        inputs=[
            InputSpec(id="syllabus", loader="source_drive_file"),
        ],
        reasoning=[
            ReasoningStep(
                id="reason",
                entry_agent="CoordinatorAgent",
                user_template="summarise: {{syllabus}}",
            ),
        ],
        emit=[
            EmitStep(id="emit_slack", adapter="slack_post_message"),
        ],
    )
    base.update(overrides)
    return ExecutionPlan(**base).with_fresh_hash()


# ===========================================================================
# Insert + get round-trip
# ===========================================================================


def test_insert_and_get_round_trip(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan()
    returned = insert_execution_plan(conn, plan)
    assert returned == plan.hash

    fetched = get_execution_plan(conn, plan.hash)
    assert fetched is not None
    assert fetched == plan


def test_round_trip_preserves_inputs(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan(
        inputs=[
            InputSpec(
                id="syllabus",
                loader="source_drive_file",
                args={"drive_file_id": "1abc"},
                cache_for_seconds=600,
            ),
            InputSpec(
                id="bq_data",
                loader="source_bigquery",
                args={"sql": "SELECT *"},
            ),
        ],
    )
    insert_execution_plan(conn, plan)
    fetched = get_execution_plan(conn, plan.hash)
    assert fetched.inputs == plan.inputs


def test_round_trip_preserves_reasoning_with_full_options(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan(
        reasoning=[
            ReasoningStep(
                id="reason",
                description="describe + tag",
                entry_agent="CoordinatorAgent",
                transfers_allowed=["AmazonHeadAgent"],
                tools=["slack_post_message", "graph_lookup"],
                tool_mode=ToolMode.WRITE_ALLOWED,
                model="anthropic/claude-opus-4-7",
                user_template="t",
                output=OutputSpec(type="json", schema={"type": "object"}),
                retry=Retry(on_validation_fail=2, on_tool_error=3),
                max_tool_calls=15,
            ),
        ],
    )
    insert_execution_plan(conn, plan)
    fetched = get_execution_plan(conn, plan.hash)
    assert fetched.reasoning == plan.reasoning


def test_round_trip_preserves_emit_with_gate(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan(
        emit=[
            EmitStep(
                id="emit_slack",
                adapter="slack_post_message",
                args={"channel": "amazon-team"},
                gate=Gate(type="sheet_dedup", args={"window_days": 7}),
                abort_on_gate_fail=True,
            ),
        ],
    )
    insert_execution_plan(conn, plan)
    fetched = get_execution_plan(conn, plan.hash)
    assert fetched.emit == plan.emit


def test_round_trip_preserves_acceptance(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan(
        acceptance=Acceptance(checks=["sentiment_positive", "len_under_500"]),
    )
    insert_execution_plan(conn, plan)
    fetched = get_execution_plan(conn, plan.hash)
    assert fetched.acceptance == plan.acceptance


def test_round_trip_preserves_on_failure(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan(
        on_failure=FailureAction(
            action=FailureActionType.RETRY_LATER,
            notify=["sergey@mellanni.com"],
            retry_after_minutes=30,
            abort=False,
        ),
    )
    insert_execution_plan(conn, plan)
    fetched = get_execution_plan(conn, plan.hash)
    assert fetched.on_failure == plan.on_failure


def test_round_trip_preserves_enforcement_strict(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan(enforcement=EnforcementMode.STRICT)
    insert_execution_plan(conn, plan)
    fetched = get_execution_plan(conn, plan.hash)
    assert fetched.enforcement is EnforcementMode.STRICT


def test_round_trip_preserves_enforcement_permissive(tmp_path):
    """The validator chokepoint rejects PERMISSIVE at freeze
    time, but the storage layer must round-trip it for replay
    / migration tooling."""
    conn = _migrate(tmp_path)
    plan = _baseline_plan(enforcement=EnforcementMode.PERMISSIVE)
    insert_execution_plan(conn, plan)
    fetched = get_execution_plan(conn, plan.hash)
    assert fetched.enforcement is EnforcementMode.PERMISSIVE


def test_round_trip_preserves_parent_hash(tmp_path):
    conn = _migrate(tmp_path)
    plan = _baseline_plan()
    revised = plan.model_copy(
        update={"parent_hash": plan.hash}
    ).with_fresh_hash()
    insert_execution_plan(conn, plan)
    insert_execution_plan(conn, revised)
    fetched = get_execution_plan(conn, revised.hash)
    assert fetched.parent_hash == plan.hash


# ===========================================================================
# get edge cases
# ===========================================================================


def test_get_missing_hash_returns_none(tmp_path):
    conn = _migrate(tmp_path)
    assert get_execution_plan(conn, "a" * 64) is None


# ===========================================================================
# Immutability + uniqueness
# ===========================================================================


def test_insert_duplicate_hash_raises_integrity_error(tmp_path):
    """Plans are content-addressed; duplicate hash means the
    same body was inserted twice. Fail loud so the caller knows
    to call get_execution_plan first."""
    conn = _migrate(tmp_path)
    plan = _baseline_plan()
    insert_execution_plan(conn, plan)
    with pytest.raises(sqlite3.IntegrityError):
        insert_execution_plan(conn, plan)


def test_module_exposes_no_update_or_delete_helper():
    """Plans are immutable; the module surface must not include
    update_* / delete_* helpers, even for tests."""
    public = {
        name for name in dir(plans_mod) if not name.startswith("_")
    }
    forbidden = {
        "update_execution_plan",
        "delete_execution_plan",
        "update_plan",
        "delete_plan",
        "drop_plan",
    }
    leaked = public & forbidden
    assert not leaked, (
        f"execution_plans module exposes update/delete helpers: "
        f"{sorted(leaked)}. Plans are immutable in v2."
    )


# ===========================================================================
# Freeze guard
# ===========================================================================


def test_insert_refuses_draft_without_hash(tmp_path):
    conn = _migrate(tmp_path)
    draft = ExecutionPlan(
        id="draft_plan",
        description="this plan is unfrozen",
        author="sergey@mellanni.com",
        emit=[EmitStep(id="emit_one", adapter="slack_post_message")],
    )
    assert draft.hash == ""
    with pytest.raises(ExecutionPlanNotFrozenError, match="draft_plan"):
        insert_execution_plan(conn, draft)


# ===========================================================================
# DDL CHECK
# ===========================================================================


def test_enforcement_check_rejects_unknown_value(tmp_path):
    """The enforcement column has a CHECK constraint
    (strict|permissive). The Pydantic enum already blocks
    other values at construction; raw SQL exercises the
    DB-side guard."""
    conn = _migrate(tmp_path)
    plan = _baseline_plan()
    insert_execution_plan(conn, plan)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE execution_plans SET enforcement = 'lax' "
            "WHERE hash = ?",
            (plan.hash,),
        )


# ===========================================================================
# Connection guard
# ===========================================================================


def test_insert_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        insert_execution_plan(bare, _baseline_plan())


def test_get_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        get_execution_plan(bare, "a" * 64)


# ===========================================================================
# Smoke checks
# ===========================================================================


def test_execution_plans_module_has_no_io_imports():
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
    for _, member in vars(plans_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"execution_plans module imports I/O libs: {sorted(leaked)}."
    )


def test_execution_plans_module_has_no_dispatch_callables():
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
    for name, member in vars(plans_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"execution_plans module exposes execution / claim "
                f"suggestive callable: {name}"
            )
