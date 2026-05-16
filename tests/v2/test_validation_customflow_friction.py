"""Phase 12 slice 3 — §5.9 CustomFlow friction SIGNAL (Q5
subset; reviewer Option A + C1–C7).

- C1: DISTINCT code ``customflow_admin_approval_advisory``,
  severity=``warning`` (NEVER ``error``).
- C2: triggers STRICTLY on a ``tool_mode=write_allowed`` step
  OR a tool tagged privileged/costly/filesystem_write
  (``tool_tags.requires_admin_approval``). 4 orthogonal
  triggers out of scope.
- C3: §11.1 early-no-op + independent NON-short-circuit
  (all-issues-collected).
- C4: no parallel path — the shipped ValidationIssue warning
  surface; slice-1 establishes write_allowed⇒allowed (no
  reject), friction raised separately as a warning.
- C5: no dangling-gate illusion — the message states SIGNAL
  ONLY / no executor / DEFERRED.
- Boundary: slice-2 = ERROR (reject, ok False);
  slice-3 = WARNING (friction, ok True). A ``write_allowed``
  step is allowed by slice-1/2 but TRIPS slice-3 friction.
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
_FRICTION = "customflow_admin_approval_advisory"
_OWNER = UserRef(platform="slack", user_id="U1", display_name="S")
_DELIVERY = Delivery(
    target_session_id="sl_C1",
    fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
)
_FAILURE = FailurePolicy(
    on_failure_action=FailureActionType.ALERT_ADMIN
)
_AUDIT = AuditPolicy()


def _step(sid, tools, *, mode=ToolMode.READ_ONLY):
    return ReasoningStep(
        id=sid,
        entry_agent="CoordinatorAgent",
        tools=tools,
        tool_mode=mode,
        user_template="t",
    )


def _plan(steps):
    return ExecutionPlan(
        id="p",
        description="reasoning plan body",
        author="tester",
        inputs=[InputSpec(id="src", loader="source_literal")],
        reasoning=steps,
        emit=[EmitStep(id="e", adapter="slack_post_message")],
    ).with_fresh_hash()


def _emit_only_plan():
    return ExecutionPlan(
        id="p",
        description="emit only plan body",
        author="tester",
        inputs=[InputSpec(id="src", loader="source_literal")],
        reasoning=[],
        emit=[EmitStep(id="e", adapter="slack_post_message")],
    ).with_fresh_hash()


def _cron_spec(plan_hash):
    return ScheduleSpec(
        id="s",
        owner=_OWNER,
        description="cron friction enforcement spec",
        trigger=CronTrigger(cron="0 9 * * *", timezone="UTC"),
        delivery=_DELIVERY,
        failure=_FAILURE,
        audit=_AUDIT,
        execution_plan_hash=plan_hash,
    ).with_fresh_hash()


def _reg(*descriptors):
    tools = ToolRegistry()
    for d in descriptors:
        tools.register(d)
    return RegistrySnapshot(tools=tools)


def _tool(name, tags):
    return ToolDescriptor(
        name=name,
        description="tool descriptor",
        tags=tags,
        module="app.v2.adapters.x",
    )


def _issues(spec, **kw):
    return validate_schedule_spec(spec, **kw).issues


# ---------------------------------------------------------------------------
# write_allowed step ⇒ WARNING friction, ok stays True (C1/C4)
# ---------------------------------------------------------------------------


def test_write_allowed_step_emits_warning_not_error():
    plan = _plan([_step("a", ["x"], mode=ToolMode.WRITE_ALLOWED)])
    spec = _cron_spec(plan.hash)
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}
    )  # registries=None — write_allowed trigger needs no registry
    warn = [i for i in res.warnings() if i.code == _FRICTION]
    assert len(warn) == 1
    assert warn[0].severity == "warning"
    assert _FRICTION not in [i.code for i in res.errors()]
    # FRICTION, not REJECT — the spec is not error-blocked by it.
    assert all(
        i.code != _FRICTION for i in res.errors()
    )


# ---------------------------------------------------------------------------
# Tag trigger — privileged / costly / filesystem_write (C2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [T.PRIVILEGED, T.COSTLY, T.FILESYSTEM_WRITE],
)
def test_admin_approval_tag_trips_friction_warning(tag):
    # write_allowed so slice-2 does not also hard-reject — we
    # isolate the slice-3 tag trigger.
    plan = _plan(
        [_step("a", ["risky"], mode=ToolMode.WRITE_ALLOWED)]
    )
    spec = _cron_spec(plan.hash)
    reg = _reg(_tool("risky", {tag}))
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=reg
    )
    assert any(i.code == _FRICTION for i in res.warnings())


def test_costly_readonly_tool_friction_without_slice2_error():
    """COSTLY is NOT in the slice-2 read_only block set — a
    read_only step using a costly tool is NOT rejected, but
    DOES trip slice-3 friction. Clean boundary case."""
    plan = _plan([_step("a", ["bq"])])  # read_only
    spec = _cron_spec(plan.hash)
    reg = _reg(_tool("bq", {T.READ_EXTERNAL, T.COSTLY}))
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=reg
    )
    err_codes = [i.code for i in res.errors()]
    assert "reasoning_tool_write_in_read_only" not in err_codes
    assert "reasoning_tool_unresolved" not in err_codes
    assert any(i.code == _FRICTION for i in res.warnings())


def test_non_admin_tag_does_not_trip_friction():
    plan = _plan(
        [_step("a", ["reader"], mode=ToolMode.WRITE_ALLOWED)]
    )
    # WRITE_ALLOWED already trips friction; use read_only to
    # isolate the tag predicate.
    plan = _plan([_step("a", ["reader"])])
    spec = _cron_spec(plan.hash)
    reg = _reg(_tool("reader", {T.READ_EXTERNAL}))
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}, registries=reg
    )
    assert not any(i.code == _FRICTION for i in res.warnings())


# ---------------------------------------------------------------------------
# slice-2-REJECT vs slice-3-FRICTION boundary (explicit pin)
# ---------------------------------------------------------------------------


def test_boundary_reject_is_error_friction_is_warning():
    reg = _reg(_tool("poster", {T.WRITE_EXTERNAL}))

    # slice-2: read_only + write tool ⇒ ERROR, ok False.
    ro_plan = _plan([_step("a", ["poster"])])
    ro = validate_schedule_spec(
        _cron_spec(ro_plan.hash),
        execution_plans={ro_plan.hash: ro_plan},
        registries=reg,
    )
    assert ro.ok is False
    assert "reasoning_tool_write_in_read_only" in [
        i.code for i in ro.errors()
    ]

    # slice-3: write_allowed (allowed by slice-1/2) ⇒ WARNING,
    # ok True — friction, not reject.
    wa_plan = _plan(
        [_step("a", ["poster"], mode=ToolMode.WRITE_ALLOWED)]
    )
    wa = validate_schedule_spec(
        _cron_spec(wa_plan.hash),
        execution_plans={wa_plan.hash: wa_plan},
        registries=reg,
    )
    assert wa.ok is True
    assert any(i.code == _FRICTION for i in wa.warnings())
    assert "reasoning_tool_write_in_read_only" not in [
        i.code for i in wa.errors()
    ]


# ---------------------------------------------------------------------------
# C5 — no dangling-gate illusion (message semantic accuracy)
# ---------------------------------------------------------------------------


def test_friction_message_states_signal_only_no_executor():
    plan = _plan([_step("a", ["x"], mode=ToolMode.WRITE_ALLOWED)])
    spec = _cron_spec(plan.hash)
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}
    )
    msg = next(
        i.message for i in res.warnings() if i.code == _FRICTION
    )
    assert "ADVISORY SIGNAL ONLY" in msg
    assert "no admin-approval gate or executor is shipped" in msg
    assert "DEFERRED" in msg
    assert "NOT blocked" in msg


# ---------------------------------------------------------------------------
# C3 — §11.1 early-no-op + all-issues-collected
# ---------------------------------------------------------------------------


def test_emit_only_plan_no_friction_even_registries_none():
    plan = _emit_only_plan()
    spec = _cron_spec(plan.hash)
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}
    )
    assert not any(i.code == _FRICTION for i in res.issues)


def test_no_plan_index_is_early_no_op():
    spec = _cron_spec("a" * 64)
    assert not any(
        i.code == _FRICTION for i in validate_schedule_spec(spec).issues
    )


def test_friction_collected_alongside_other_issues():
    """Non-short-circuit: a tampered-hash spec (error) with a
    write_allowed friction step surfaces BOTH the hash error
    AND the friction warning."""
    plan = _plan([_step("a", ["x"], mode=ToolMode.WRITE_ALLOWED)])
    spec = _cron_spec(plan.hash)
    tampered = spec.model_copy(update={"hash": "b" * 64})
    res = validate_schedule_spec(
        tampered, execution_plans={plan.hash: plan}
    )
    codes = [i.code for i in res.issues]
    assert "hash_mismatch" in codes
    assert _FRICTION in codes
    assert res.ok is False  # error present (the hash), warning rides along


def test_every_friction_step_collected_with_path_index():
    plan = _plan(
        [
            _step("a", ["x"], mode=ToolMode.WRITE_ALLOWED),
            _step("b", ["y"]),  # read_only, no friction
            _step("c", ["z"], mode=ToolMode.WRITE_ALLOWED),
        ]
    )
    spec = _cron_spec(plan.hash)
    res = validate_schedule_spec(
        spec, execution_plans={plan.hash: plan}
    )
    paths = sorted(
        i.path for i in res.warnings() if i.code == _FRICTION
    )
    assert paths == [
        "execution_plan.reasoning[0]",
        "execution_plan.reasoning[2]",
    ]


# ---------------------------------------------------------------------------
# Distinct code from slice-2
# ---------------------------------------------------------------------------


def test_distinct_from_slice2_codes():
    assert _FRICTION != "reasoning_tool_write_in_read_only"
    assert _FRICTION != "reasoning_tool_unresolved"
