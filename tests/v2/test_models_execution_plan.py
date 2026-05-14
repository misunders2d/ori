"""Tests for ``app.v2.models.execution_plan``.

Pins:
- ``ExecutionPlan`` requires at least one emit (else useless).
- ``EmitStep.id`` is required (idempotency key foundation —
  round-6 round of the design review).
- ``ReasoningStep.tool_mode`` defaults to ``read_only``.
- ``enforcement`` defaults to ``strict``.
- Hash determinism + canonicalisation excludes ``hash`` and
  ``authored_at``.
- Snake_case id pattern applied to ExecutionPlan,
  ReasoningStep, InputSpec, EmitStep.
- All nested shapes (Retry, Acceptance, FailureAction, Gate)
  validate.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0
- ``docs/PHASE_1_PLAN.md`` §4.5 / §5.1
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.v2.enums import EnforcementMode, FailureActionType, ToolMode
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


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _minimal_plan_kwargs(**overrides):
    base = dict(
        id="daily_audit",
        description="Daily ASIN audit.",
        author="sergey@mellanni.com",
        emit=[
            EmitStep(
                id="post_slack",
                adapter="slack_post",
                args={"channel": "C012", "content": "done"},
            )
        ],
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# EmitStep — id required
# ---------------------------------------------------------------------------


def test_emit_step_id_required():
    """Round-6 correction: EmitStep.id is REQUIRED so the emit
    idempotency key ``schedule_id:root_run_id:emit_id`` is
    stable across retries. v1's optional id is rejected."""
    with pytest.raises(ValidationError):
        EmitStep(adapter="slack_post", args={})


def test_emit_step_id_must_be_snake_case():
    with pytest.raises(ValidationError):
        EmitStep(id="PostSlack", adapter="slack_post", args={})
    with pytest.raises(ValidationError):
        EmitStep(id="post-slack", adapter="slack_post", args={})


def test_emit_step_id_accepts_valid_snake_case():
    e = EmitStep(id="post_slack_v2", adapter="slack_post", args={"x": 1})
    assert e.id == "post_slack_v2"


def test_emit_step_gate_optional():
    e = EmitStep(id="post", adapter="slack_post", args={})
    assert e.gate is None
    assert e.abort_on_gate_fail is False


def test_emit_step_with_gate():
    e = EmitStep(
        id="post",
        adapter="slack_post",
        args={},
        gate=Gate(type="sheet_dedup", args={"key": "today"}),
        abort_on_gate_fail=True,
    )
    assert e.gate is not None
    assert e.abort_on_gate_fail is True


def test_emit_step_forbids_extra_fields():
    with pytest.raises(ValidationError):
        EmitStep(
            id="post", adapter="slack_post", args={}, retries=3
        )


# ---------------------------------------------------------------------------
# ReasoningStep — defaults + validators
# ---------------------------------------------------------------------------


def test_reasoning_step_tool_mode_defaults_to_read_only():
    r = ReasoningStep(
        id="step_1",
        entry_agent="BigQueryAgent",
        user_template="Analyse {sales}",
    )
    assert r.tool_mode == ToolMode.READ_ONLY


def test_reasoning_step_accepts_write_allowed_opt_in():
    r = ReasoningStep(
        id="step_1",
        entry_agent="BigQueryAgent",
        user_template="x",
        tool_mode=ToolMode.WRITE_ALLOWED,
    )
    assert r.tool_mode == ToolMode.WRITE_ALLOWED


def test_reasoning_step_id_snake_case():
    with pytest.raises(ValidationError):
        ReasoningStep(
            id="Step1",
            entry_agent="X",
            user_template="x",
        )


def test_reasoning_step_user_template_required():
    with pytest.raises(ValidationError):
        ReasoningStep(id="step_1", entry_agent="X")


def test_reasoning_step_output_defaults_to_json_type():
    r = ReasoningStep(id="step_1", entry_agent="X", user_template="x")
    assert r.output.type == "json"


def test_reasoning_step_max_tool_calls_bounded():
    with pytest.raises(ValidationError):
        ReasoningStep(
            id="step_1",
            entry_agent="X",
            user_template="x",
            max_tool_calls=0,
        )
    with pytest.raises(ValidationError):
        ReasoningStep(
            id="step_1",
            entry_agent="X",
            user_template="x",
            max_tool_calls=500,
        )


# ---------------------------------------------------------------------------
# InputSpec
# ---------------------------------------------------------------------------


def test_input_spec_id_snake_case():
    with pytest.raises(ValidationError):
        InputSpec(id="Sales-30d", loader="bigquery_query")


def test_input_spec_cache_for_seconds_default_zero():
    i = InputSpec(id="sales_30d", loader="bigquery_query")
    assert i.cache_for_seconds == 0


def test_input_spec_cache_for_seconds_non_negative():
    with pytest.raises(ValidationError):
        InputSpec(id="x", loader="bigquery_query", cache_for_seconds=-1)


# ---------------------------------------------------------------------------
# OutputSpec
# ---------------------------------------------------------------------------


def test_output_spec_type_constrained():
    with pytest.raises(ValidationError):
        OutputSpec(type="binary")


def test_output_spec_schema_alias():
    """``schema_`` is the Python identifier (avoids shadowing the
    builtin); JSON I/O uses the ``schema`` alias."""
    o = OutputSpec(type="json", **{"schema": {"type": "object"}})
    assert o.schema_ == {"type": "object"}


def test_output_spec_none_type_allowed_at_model_level():
    """Phase 1: ``type='none'`` accepted at the Pydantic level;
    the rigor validator (phase 2) rejects it at freeze. Two
    layers, one concern."""
    o = OutputSpec(type="none")
    assert o.type == "none"


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def test_retry_defaults():
    r = Retry()
    assert r.on_validation_fail == 1
    assert r.on_tool_error == 2


def test_retry_bounded():
    with pytest.raises(ValidationError):
        Retry(on_validation_fail=-1)
    with pytest.raises(ValidationError):
        Retry(on_validation_fail=6)
    with pytest.raises(ValidationError):
        Retry(on_tool_error=6)


# ---------------------------------------------------------------------------
# ExecutionPlan — required + defaults
# ---------------------------------------------------------------------------


def test_execution_plan_requires_at_least_one_emit():
    with pytest.raises(ValidationError, match="at least one emit"):
        ExecutionPlan(
            id="x",
            description="x",
            author="x",
            emit=[],
        )


def test_execution_plan_id_snake_case():
    with pytest.raises(ValidationError):
        ExecutionPlan(**_minimal_plan_kwargs(id="DailyAudit"))


def test_execution_plan_enforcement_defaults_strict():
    p = ExecutionPlan(**_minimal_plan_kwargs())
    assert p.enforcement == EnforcementMode.STRICT


def test_execution_plan_accepts_permissive_at_model_level():
    """The freeze pathway (phase-2 rigor validator) rejects
    permissive. The model itself accepts it for testing /
    authoring round-trips."""
    p = ExecutionPlan(
        **_minimal_plan_kwargs(enforcement=EnforcementMode.PERMISSIVE)
    )
    assert p.enforcement == EnforcementMode.PERMISSIVE


def test_execution_plan_version_minimum_one():
    with pytest.raises(ValidationError):
        ExecutionPlan(**_minimal_plan_kwargs(version=0))


def test_execution_plan_on_failure_defaults():
    p = ExecutionPlan(**_minimal_plan_kwargs())
    assert p.on_failure.action == FailureActionType.ALERT_ADMIN
    assert p.on_failure.notify == []
    assert p.on_failure.abort is True
    assert p.on_failure.retry_after_minutes == 60


def test_execution_plan_acceptance_defaults_empty():
    p = ExecutionPlan(**_minimal_plan_kwargs())
    assert p.acceptance.checks == []


# ---------------------------------------------------------------------------
# Hash determinism
# ---------------------------------------------------------------------------


def test_execution_plan_compute_hash_deterministic():
    p = ExecutionPlan(**_minimal_plan_kwargs())
    assert p.compute_hash() == p.compute_hash()
    assert len(p.compute_hash()) == 64


def test_execution_plan_hash_excludes_authored_at():
    a = ExecutionPlan(
        **_minimal_plan_kwargs(authored_at="2026-01-01T00:00:00Z")
    )
    b = ExecutionPlan(
        **_minimal_plan_kwargs(authored_at="2027-12-31T23:59:59Z")
    )
    assert a.compute_hash() == b.compute_hash()


def test_execution_plan_hash_excludes_hash_field():
    a = ExecutionPlan(**_minimal_plan_kwargs())
    b = ExecutionPlan(**_minimal_plan_kwargs(hash="cafebabe" * 8))
    assert a.compute_hash() == b.compute_hash()


def test_execution_plan_hash_changes_when_emit_changes():
    a = ExecutionPlan(**_minimal_plan_kwargs())
    b = ExecutionPlan(
        **_minimal_plan_kwargs(
            emit=[
                EmitStep(
                    id="post_slack",
                    adapter="slack_post",
                    args={"channel": "C012", "content": "different"},
                )
            ]
        )
    )
    assert a.compute_hash() != b.compute_hash()


def test_execution_plan_with_fresh_hash_populates_hash_field():
    p = ExecutionPlan(**_minimal_plan_kwargs())
    assert p.hash == ""
    refreshed = p.with_fresh_hash()
    assert refreshed.hash == p.compute_hash()


# ---------------------------------------------------------------------------
# FailureAction + Acceptance
# ---------------------------------------------------------------------------


def test_failure_action_action_must_be_enum():
    with pytest.raises(ValidationError):
        FailureAction(action="ignore")


def test_failure_action_retry_after_minimum_one_minute():
    with pytest.raises(ValidationError):
        FailureAction(retry_after_minutes=0)


def test_acceptance_accepts_empty_checks():
    a = Acceptance()
    assert a.checks == []


def test_acceptance_checks_list_of_strings():
    a = Acceptance(checks=["must contain ASIN", "min 100 chars"])
    assert len(a.checks) == 2


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_gate_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Gate(type="sheet_dedup", args={}, retries=1)


def test_gate_args_default_empty():
    g = Gate(type="always_pass")
    assert g.args == {}


# ---------------------------------------------------------------------------
# Composite — a realistic ExecutionPlan
# ---------------------------------------------------------------------------


def test_realistic_plan_constructs_and_hashes():
    """End-to-end smoke: a plan with inputs + reasoning + emit +
    gate constructs cleanly and hashes deterministically."""
    p = ExecutionPlan(
        id="fba_audit",
        description="Daily FBA listing audit.",
        author="sergey@mellanni.com",
        inputs=[
            InputSpec(id="sales_30d", loader="bigquery_query", args={"sql": "..."}),
            InputSpec(id="competitors", loader="keepa_get_history", args={"asin": "B0..."})
        ],
        reasoning=[
            ReasoningStep(
                id="analyse_sales",
                entry_agent="BigQueryAgent",
                user_template="Analyse {sales_30d}",
                output=OutputSpec(
                    type="json",
                    **{"schema": {"type": "object", "required": ["summary"]}}
                ),
            )
        ],
        emit=[
            EmitStep(
                id="log_sheet",
                adapter="sheet_append",
                args={
                    "spreadsheet_id": "10cSeLai",
                    "row": ["{today}", "{analyse_sales.summary}"],
                },
                gate=Gate(type="sheet_dedup", args={"key": "{today}"}),
            ),
            EmitStep(
                id="post_slack",
                adapter="slack_post",
                args={
                    "channel": "C012ABCDE",
                    "content": "Audit complete.",
                },
            ),
        ],
        enforcement=EnforcementMode.STRICT,
    )
    assert len(p.emit) == 2
    assert p.emit[0].gate is not None
    assert p.emit[1].id == "post_slack"
    assert p.compute_hash() == p.compute_hash()
