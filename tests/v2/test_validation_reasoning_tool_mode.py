"""Phase 12 slice 2 — `_validate_reasoning_tool_mode`.

Per ``docs/PHASE_12_PLAN.md`` §1.1 / §7 / §9 (Q1–Q6) + the
claude-reviewer slice-2 hard-checks:

- Reuses the slice-1 ``evaluate_reasoning_step`` via the
  ``RegistrySnapshot.tools`` seam (Q2). ``ToolRegistry.lookup``
  (NOT ``tags_for``) so an unknown tool ⇒ ``None`` ⇒ the
  DISTINCT *unresolved* code, not conflated with *blocked*.
- ``registries=None`` (every shipped caller, §1.1a) ⇒ fail-safe
  blanket-BLOCK of any ``read_only`` reasoning-bearing plan —
  INTENDED, documented, NEVER silent-allow.
- §11.1 backward-compat: an emit-only plan / ``reasoning == []``
  / a no-plan reminder ⇒ EARLY NO-OP (zero reasoning issues).
- Distinct codes ``reasoning_tool_write_in_read_only`` vs
  ``reasoning_tool_unresolved``.
- Q6: one independent, NON-short-circuiting additive rule —
  all-issues-collected preserved (a malformed plan still
  surfaces ALL issues, not only the reasoning one).
"""

from __future__ import annotations

import pytest

from app.v2.descriptors.tool import ToolDescriptor
from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    ToolMode,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.execution_plan import (
    EmitStep,
    ExecutionPlan,
    InputSpec,
    ReasoningStep,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import CronTrigger
from app.v2.registry import ToolRegistry
from app.v2.tool_tags import ToolCapabilityTag
from app.v2.validation import RegistrySnapshot, validate_schedule_spec

T = ToolCapabilityTag
_OWNER = UserRef(platform="slack", user_id="U1", display_name="S")
_DELIVERY = Delivery(
    target_session_id="sl_C1",
    fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
)
_FAILURE = FailurePolicy(
    on_failure_action=FailureActionType.ALERT_ADMIN
)
_AUDIT = AuditPolicy()


def _plan(steps: list[ReasoningStep]) -> ExecutionPlan:
    return ExecutionPlan(
        id="p",
        description="reasoning plan body",
        author="tester",
        inputs=[InputSpec(id="src", loader="source_literal")],
        reasoning=steps,
        emit=[EmitStep(id="e", adapter="slack_post_message")],
    ).with_fresh_hash()


def _emit_only_plan() -> ExecutionPlan:
    return ExecutionPlan(
        id="p",
        description="reasoning plan body",
        author="tester",
        inputs=[InputSpec(id="src", loader="source_literal")],
        reasoning=[],
        emit=[EmitStep(id="e", adapter="slack_post_message")],
    ).with_fresh_hash()


def _step(
    sid: str,
    tools: list[str],
    *,
    mode: ToolMode = ToolMode.READ_ONLY,
) -> ReasoningStep:
    return ReasoningStep(
        id=sid,
        entry_agent="CoordinatorAgent",
        tools=tools,
        tool_mode=mode,
        user_template="t",
    )


def _cron_spec(plan_hash: str | None) -> ScheduleSpec:
    return ScheduleSpec(
        id="s",
        owner=_OWNER,
        description="cron reasoning enforcement spec",
        trigger=CronTrigger(cron="0 9 * * *", timezone="UTC"),
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
        execution_plan_hash=plan_hash,
    ).with_fresh_hash()


def _reminder_spec() -> ScheduleSpec:
    return ScheduleSpec(
        id="r",
        owner=_OWNER,
        description="reminder spec with no plan",
        trigger=CronTrigger(cron="0 9 * * *", timezone="UTC"),
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
        execution_plan_hash=None,
    ).with_fresh_hash()


def _reg(*descriptors: ToolDescriptor) -> RegistrySnapshot:
    tools = ToolRegistry()
    for d in descriptors:
        tools.register(d)
    return RegistrySnapshot(tools=tools)


def _tool(name: str, tags: set[ToolCapabilityTag]) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description="tool descriptor",
        tags=tags,
        module="app.v2.adapters.x",
    )


def _codes(spec, **kw):
    return [
        i.code for i in validate_schedule_spec(spec, **kw).issues
    ]


# ---------------------------------------------------------------------------
# read_only + write-tagged tool, registries present ⇒ BLOCKED
# ---------------------------------------------------------------------------


def test_read_only_write_tagged_tool_is_blocked():
    plan = _plan([_step("a", ["poster"])])
    spec = _cron_spec(plan.hash)
    reg = _reg(_tool("poster", {T.WRITE_EXTERNAL, T.SEND_MESSAGE}))
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=reg
    )
    assert res.ok is False
    codes = [i.code for i in res.errors()]
    assert "reasoning_tool_write_in_read_only" in codes
    assert "reasoning_tool_unresolved" not in codes


# ---------------------------------------------------------------------------
# registries=None ⇒ fail-safe blanket-block (§1.1b, INTENDED)
# ---------------------------------------------------------------------------


def test_registries_none_blanket_blocks_read_only_reasoning():
    plan = _plan([_step("a", ["anything"])])
    spec = _cron_spec(plan.hash)
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}
    )  # registries=None — every shipped caller (§1.1a)
    assert res.ok is False
    codes = [i.code for i in res.errors()]
    assert "reasoning_tool_unresolved" in codes
    assert "reasoning_tool_write_in_read_only" not in codes


def test_registries_present_but_tool_unregistered_is_unresolved():
    plan = _plan([_step("a", ["ghost"])])
    spec = _cron_spec(plan.hash)
    reg = _reg(_tool("other", {T.READ_EXTERNAL}))  # no "ghost"
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=reg
    )
    assert res.ok is False
    assert "reasoning_tool_unresolved" in [
        i.code for i in res.errors()
    ]


def test_untagged_tool_registration_is_structurally_impossible():
    """§5.4 'untagged ⇒ fail-safe' is enforced UPSTREAM by the
    ToolDescriptor model (``tags`` min_length=1): a *registered*
    tool can never be untagged, so at the validation layer the
    only unresolved path is 'unregistered' (lookup → None). The
    empty-set defensiveness in ``evaluate_reasoning_step`` is
    still unit-pinned in
    ``test_reasoning_enforcement.py::test_untagged_tool_empty_set_is_failsafe_blocked``;
    it just cannot be reached through ToolRegistry."""
    with pytest.raises(Exception):
        _tool("bare", set())


def test_read_only_with_clean_read_tool_passes_the_rule():
    plan = _plan([_step("a", ["reader"])])
    spec = _cron_spec(plan.hash)
    reg = _reg(_tool("reader", {T.READ_EXTERNAL, T.FILESYSTEM_READ}))
    codes = [
        i.code
        for i in validate_schedule_spec(
            spec, execution_plans={plan.hash: plan}, registries=reg
        ).errors()
    ]
    assert "reasoning_tool_write_in_read_only" not in codes
    assert "reasoning_tool_unresolved" not in codes


# ---------------------------------------------------------------------------
# write_allowed ⇒ no reasoning_tool_* (friction is slice 3, Q5)
# ---------------------------------------------------------------------------


def test_write_allowed_step_is_not_blocked_even_without_registry():
    plan = _plan(
        [_step("a", ["poster"], mode=ToolMode.WRITE_ALLOWED)]
    )
    spec = _cron_spec(plan.hash)
    codes = [
        i.code
        for i in validate_schedule_spec(
            spec, execution_plans={plan.hash: plan}
        ).errors()
    ]
    assert "reasoning_tool_write_in_read_only" not in codes
    assert "reasoning_tool_unresolved" not in codes


# ---------------------------------------------------------------------------
# §11.1 backward-compat — emit-only / no-plan ⇒ EARLY NO-OP
# ---------------------------------------------------------------------------


def test_emit_only_plan_no_reasoning_issue_even_registries_none():
    plan = _emit_only_plan()
    spec = _cron_spec(plan.hash)
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}
    )  # registries=None
    codes = [i.code for i in res.issues]
    assert "reasoning_tool_unresolved" not in codes
    assert "reasoning_tool_write_in_read_only" not in codes


def test_reminder_no_plan_hash_is_early_no_op():
    res = validate_schedule_spec(_reminder_spec())
    codes = [i.code for i in res.issues]
    assert "reasoning_tool_unresolved" not in codes
    assert "reasoning_tool_write_in_read_only" not in codes


def test_plan_hash_set_but_no_index_is_early_no_op():
    """execution_plans=None ⇒ rule cannot see a plan ⇒ early
    no-op (no false reasoning issue)."""
    spec = _cron_spec("a" * 64)
    codes = [i.code for i in validate_schedule_spec(spec).issues]
    assert "reasoning_tool_unresolved" not in codes


# ---------------------------------------------------------------------------
# Distinct codes
# ---------------------------------------------------------------------------


def test_blocked_and_unresolved_are_distinct_codes():
    assert (
        "reasoning_tool_write_in_read_only"
        != "reasoning_tool_unresolved"
    )


# ---------------------------------------------------------------------------
# Q6 — non-short-circuiting / all-issues-collected proof
# ---------------------------------------------------------------------------


def test_all_issues_collected_alongside_other_rule():
    """A spec that ALSO trips another rule (tampered hash ⇒
    hash_mismatch) AND has a read_only write-tool reasoning step
    surfaces BOTH — the reasoning rule does not short-circuit
    the others, and the others don't suppress it."""
    plan = _plan([_step("a", ["poster"])])
    spec = _cron_spec(plan.hash)
    tampered = spec.model_copy(update={"hash": "b" * 64})
    reg = _reg(_tool("poster", {T.WRITE_EXTERNAL}))
    codes = [
        i.code
        for i in validate_schedule_spec(
            tampered,
            execution_plans={plan.hash: plan},
            registries=reg,
        ).errors()
    ]
    assert "hash_mismatch" in codes
    assert "reasoning_tool_write_in_read_only" in codes


def test_every_offending_step_and_tool_collected():
    """Multi-step plan: each read_only step's each offending
    tool yields its own issue with the right path index."""
    plan = _plan(
        [
            _step("a", ["w1", "ok"]),
            _step("b", ["w2"]),
        ]
    )
    spec = _cron_spec(plan.hash)
    reg = _reg(
        _tool("w1", {T.WRITE_EXTERNAL}),
        _tool("w2", {T.PRIVILEGED}),
        _tool("ok", {T.READ_EXTERNAL}),
    )
    issues = [
        i
        for i in validate_schedule_spec(
            spec, execution_plans={plan.hash: plan}, registries=reg
        ).errors()
        if i.code == "reasoning_tool_write_in_read_only"
    ]
    paths = sorted(i.path for i in issues)
    assert paths == [
        "execution_plan.reasoning[0].tools",
        "execution_plan.reasoning[1].tools",
    ]
