"""Phase 12 slice 4 — worker reasoning runtime-guard SEAM.

Per ``docs/PHASE_12_PLAN.md`` §1 item 4 / §9 (Q1a/Q3) + the
claude-reviewer slice-4 hard-checks. Build-the-layer: the
guard is wired at the seam where a future LLM reasoning-chain
executor WOULD consult it, but is NOT fired on the live path.

- ``_reasoning_runtime_guard`` is reachable + strictly
  delegates to the slice-1/4 pure layer (registry-less ⇒
  §5.4 fail-safe — the worker owns no ToolRegistry).
- It is DEAD on the live fire path: a source-scan pins that
  ``_dispatch_emit_branch`` never calls it (the phase-11
  ``_fail_run`` boundary returns first).
- The phase-11 reason CODE
  ``reasoning_unsupported_pending_step_12`` is BYTE-IDENTICAL;
  the refined message must NOT imply the executor exists
  (no-dangling-gate — the phase-9/10/11 lesson).

The authoritative BEHAVIOURAL regression pin for the live
boundary is
``test_runtime_source_fire.py::test_reasoning_bearing_plan_fail_run_not_raise``
(reasoning-bearing plan ⇒ ``_fail_run`` with the byte-frozen
code) — it stays UNMODIFIED and green; not duplicated here.
"""

from __future__ import annotations

import ast
import inspect
import re
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone

from app.v2.enums import ToolMode
from app.v2.models.execution_plan import (
    EmitStep,
    ExecutionPlan,
    InputSpec,
    ReasoningStep,
)
from app.v2.reasoning_enforcement import (
    ReasoningPlanGuardResult,
    evaluate_reasoning_step,
)
from app.v2.runtime.worker import Worker

_NOW = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _worker() -> Worker:
    return Worker(
        conn_factory=lambda: sqlite3.connect(":memory:"),
        worker_id="w-seam",
        poll_interval=timedelta(seconds=10),
        clock=lambda: _NOW,
        run_id_factory=lambda: "nope",
        event_id_factory=lambda: "evt",
    )


def _step(sid: str, tools, *, mode=ToolMode.READ_ONLY):
    return ReasoningStep(
        id=sid,
        entry_agent="CoordinatorAgent",
        tools=tools,
        tool_mode=mode,
        user_template="t",
    )


def _plan(steps):
    return ExecutionPlan(
        id="p_seam",
        description="reasoning seam plan",
        author="tester",
        inputs=[InputSpec(id="src", loader="source_literal")],
        reasoning=steps,
        emit=[EmitStep(id="e", adapter="slack_post_message")],
    ).with_fresh_hash()


# ---------------------------------------------------------------------------
# Reachable + strict slice-1 delegation, registry-less fail-safe
# ---------------------------------------------------------------------------


def test_guard_reachable_and_delegates_registry_less_failsafe():
    w = _worker()
    ro = _step("ro", ["some_tool"])  # read_only + a tool
    wa = _step("wa", ["writer"], mode=ToolMode.WRITE_ALLOWED)
    plan = _plan([ro, wa])

    result = w._reasoning_runtime_guard(plan)

    assert isinstance(result, ReasoningPlanGuardResult)
    assert [sid for sid, _o in result.outcomes] == ["ro", "wa"]
    # Registry-less: the worker owns no ToolRegistry, so the
    # guard's resolver returns None for every tool ⇒ §5.4
    # fail-safe. Outcomes MUST equal calling slice-1 directly
    # with a None-resolver (strict delegation, no reimpl).
    assert result.outcomes[0][1] == evaluate_reasoning_step(
        ro, resolve_tags=lambda _n: None
    )
    assert result.outcomes[1][1] == evaluate_reasoning_step(
        wa, resolve_tags=lambda _n: None
    )
    # ro: read_only + unresolved tool ⇒ blocked; wa:
    # write_allowed ⇒ allowed. Not all allowed.
    assert result.outcomes[0][1].allowed is False
    assert result.outcomes[0][1].unresolved_tools == ("some_tool",)
    assert result.outcomes[1][1].allowed is True
    assert result.all_allowed is False


def test_guard_empty_reasoning_is_all_allowed():
    w = _worker()
    assert w._reasoning_runtime_guard(_plan([])).all_allowed is True


# ---------------------------------------------------------------------------
# DEAD on the live path — source-scan pins it (build-the-layer)
# ---------------------------------------------------------------------------


def test_guard_not_invoked_on_live_dispatch_path():
    """AST (NOT substring — the seam COMMENT legitimately
    names the guard): no actual Call to
    ``self._reasoning_runtime_guard(...)`` exists anywhere in
    ``_dispatch_emit_branch``. The guard stays DEAD on the
    live fire path until a reasoning executor lands (Q1a/Q3)."""
    src = textwrap.dedent(
        inspect.getsource(Worker._dispatch_emit_branch)
    )
    tree = ast.parse(src)
    called = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(
            node.func, ast.Attribute
        ):
            if node.func.attr == "_reasoning_runtime_guard":
                called = True
    assert not called, (
        "the runtime guard must remain DEAD on the live fire "
        "path — _dispatch_emit_branch must not CALL it (the "
        "seam comment naming it is expected)"
    )


# ---------------------------------------------------------------------------
# Reason CODE byte-identical + refined message no-executor-implication
# ---------------------------------------------------------------------------


def test_reason_code_byte_identical():
    src = inspect.getsource(Worker._dispatch_emit_branch)
    assert (
        'reason="reasoning_unsupported_pending_step_12"' in src
    ), "the phase-11 reason CODE must stay byte-identical"


def test_refined_reasoning_message_no_executor_implication():
    """Scoped to the REASONING-boundary message only. The
    CustomFlow/template-None raise in the same method
    legitimately keeps its own accurate
    'CustomFlow executor lands in §12 step 12' wording — a
    DIFFERENT message, out of slice-4 scope.

    The message is built from adjacent ``f"..."`` fragments;
    glue them (strip the ``"<nl><indent>f?"`` concatenation
    boundary) before substring checks so a line-split does
    not make the pin vacuous."""
    raw = inspect.getsource(Worker._dispatch_emit_branch)
    glued = re.sub(r'"\s*\n\s*f?"', "", raw)
    # Refined reasoning message present + accurate.
    assert "the read-only-reasoning ENFORCEMENT layer only" in glued
    assert "EXECUTOR is not built (no §12 step owns it)" in glued
    # The OLD stale reasoning phrasing is gone (it implied the
    # reasoning executor "lands" here). The CustomFlow-template
    # message ("the CustomFlow executor lands in §12 step 12")
    # is DISTINCT (preceded by 'CustomFlow', not 'reasoning')
    # and intentionally retained.
    assert "reasoning executor lands in §12 step 12" not in glued
    assert "the reasoning executor lands" not in glued
    assert "the CustomFlow executor lands in §12 step 12" in glued
