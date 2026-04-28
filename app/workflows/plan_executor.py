"""Plan-and-execute workflow — deterministic step loop.

**Mechanical guarantee** (not LLM-reliant):

- A Python `while` loop reads `has_pending_steps(session_id)` from
  `app/runtime/plan_storage.py`. The LLM cannot bypass this check.
- `get_next_step(session_id)` is called by code, not LLM. The LLM never
  picks the step or its order — the workflow claims the next pending
  step from SQLite and hands the coordinator a step prompt.
- `complete_step(session_id, result)` is called by code with the
  coordinator's response as the result. The LLM cannot skip recording —
  every step completion is written by the workflow itself.
- The coordinator only sees the current step's prompt for that turn —
  never "you have N steps, choose what's next". It can't deviate because
  it doesn't see anything to deviate from.

This replaces a previous nudge-based design where the LLM was expected
to call `get_next_step` / `complete_step` itself; missed calls would
either misfire (sub-agent calling a tool it doesn't have) or escape the
loop entirely (chatty summary mid-plan, plan stalled).

**Single-turn chat (no plan)**: the workflow runs the coordinator once
with the user's message and exits. If the coordinator creates a plan
via `create_plan`, the loop catches it on the next `has_pending_steps`
check and runs the steps deterministically.

**Ad-hoc plans + scheduled plans**: same code path. Scheduled tasks
seed the plan via `seed_plan_for_session` before the workflow starts;
the loop sees pending steps on iteration 0 and proceeds.

ADK 2.0 dynamic-workflow pattern (https://adk.dev/workflows/dynamic/):
single `@node`-decorated async function with `rerun_on_resume=True`,
calling `ctx.run_node(coordinator_agent, ...)` per step.
"""

from __future__ import annotations

import logging

from google.adk.agents.context import Context
from google.adk.workflow import Workflow, node

from app.agents.coordinator import root_agent as coordinator_agent
from app.agents.step_executor import StepResult, step_executor
from app.runtime.plan_storage import (
    abandon_plan,
    complete_step,
    get_next_step,
    has_pending_steps,
)

logger = logging.getLogger(__name__)


_MAX_ITERATIONS = 25
_RESULT_RECORD_LIMIT = 2000


def _session_id_from_ctx(ctx: Context) -> str | None:
    session = getattr(ctx, "session", None)
    if not session:
        return None
    return getattr(session, "id", None) or getattr(session, "session_id", None)


def _extract_text(response) -> str:
    """Pull a plain-text summary from whatever the coordinator emitted."""
    if response is None:
        return ""
    # Common ADK shapes: Event with .output, types.Content, str.
    for attr in ("output", "text", "content"):
        val = getattr(response, attr, None)
        if isinstance(val, str) and val:
            return val
        if val is not None and hasattr(val, "parts"):
            return " ".join(
                p.text for p in val.parts if getattr(p, "text", None)
            )
    return str(response)


def _build_step_prompt(step: dict) -> str:
    """The prompt the step_executor sees for one step.

    Behavior rules (don't improvise on failure, don't claim unconfirmed
    success, return typed pass/fail) live in step_executor's instruction
    — keeping them out of the per-step prompt avoids per-call cost.
    """
    return f"PLAN STEP {step['step_index']}: {step['description']}"


def _coerce_step_result(step_response) -> StepResult:
    """Pull a StepResult out of whatever the workflow handed back.

    Per ADK 2.0, `output_schema=StepResult` causes the agent's final
    answer to be validated against the schema; the workflow may surface
    it as the StepResult instance, a dict, or wrapped on a content/event
    object. We normalize all three so the caller can read `.status`
    without guessing.
    """
    if isinstance(step_response, StepResult):
        return step_response

    raw = getattr(step_response, "output", step_response)
    if isinstance(raw, StepResult):
        return raw
    if isinstance(raw, dict):
        try:
            return StepResult(**raw)
        except Exception:
            pass

    # Last-resort fallback: the agent returned free text instead of a
    # structured StepResult. Treat as a failure rather than silently
    # advancing — better to abort the plan than to march through with
    # an unrecognized response shape.
    text = _extract_text(step_response) or "(no response)"
    return StepResult(
        status="failed",
        summary="Unstructured response from step_executor.",
        failure_reason=f"Expected StepResult, got: {text[:300]}",
    )


async def _drive_plan_loop(ctx, response, session_id: str):
    """Code-side step loop. Pulled out of the @node-decorated wrapper so
    tests can drive it directly with a mock ctx (decorated nodes are
    pydantic FunctionNode objects, not callables).

    Each step is executed by `step_executor` (task-mode LlmAgent with
    output_schema=StepResult). The workflow reads the typed status and:
      - 'completed' → record summary, advance to next step
      - 'failed'    → record reason, abandon the plan, return the
                      failure response (caller surfaces it to the user
                      via the scheduled-task delivery path)
    No string parsing, no LLM judgment — the step result is structurally
    typed via Pydantic.
    """
    initial_pending = await has_pending_steps(session_id)
    logger.info(
        "plan_executor: entering step loop for session %s, has_pending_steps=%s",
        session_id, initial_pending,
    )
    iterations = 0
    while (
        await has_pending_steps(session_id)
        and iterations < _MAX_ITERATIONS
    ):
        iterations += 1
        step = await get_next_step(session_id)
        if not step:
            logger.info(
                "plan_executor: get_next_step returned None at iter %d — exiting loop",
                iterations,
            )
            break

        logger.info(
            "plan_executor: step %d START [session=%s] desc=%r",
            step["step_index"], session_id, step["description"][:120],
        )
        step_response = await ctx.run_node(
            step_executor, _build_step_prompt(step),
        )
        result = _coerce_step_result(step_response)
        logger.info(
            "plan_executor: step %d RESULT [session=%s] status=%s summary=%r",
            step["step_index"], session_id,
            result.status, (result.summary or "")[:200],
        )

        if result.status == "failed":
            logger.warning(
                "plan_executor: step %d FAILED for session %s — aborting plan. Reason: %s",
                step["step_index"], session_id,
                (result.failure_reason or result.summary)[:200],
            )
            failure_record = (
                f"FAILED: {result.failure_reason or result.summary}"
            )[:_RESULT_RECORD_LIMIT]
            await complete_step(session_id, failure_record)
            await abandon_plan(session_id)
            return step_response

        await complete_step(
            session_id, result.summary[:_RESULT_RECORD_LIMIT],
        )
        response = step_response

    final_pending = await has_pending_steps(session_id)
    if iterations >= _MAX_ITERATIONS and final_pending:
        logger.warning(
            "plan_executor: hit iteration cap %d for session %s",
            _MAX_ITERATIONS, session_id,
        )
    logger.info(
        "plan_executor: exiting step loop for session %s after %d iteration(s), has_pending_steps=%s",
        session_id, iterations, final_pending,
    )

    return response


@node(name="plan_executor", rerun_on_resume=True)
async def plan_executor_node(ctx: Context, node_input):
    """Run the coordinator once on user input, then drive any active plan
    through to completion via a code-side step loop.

    Per ADK 2.0 contract, dynamic-scheduling parents (i.e. nodes that call
    `ctx.run_node(...)`) must set `rerun_on_resume=True`; the runtime
    re-runs the parent on resume and replays cached child outputs.
    """
    # First turn: run the coordinator with the user's input. The coordinator
    # may create a plan via `create_plan` during this turn; the loop below
    # catches it.
    response = await ctx.run_node(coordinator_agent, node_input)

    session_id = _session_id_from_ctx(ctx)
    if not session_id:
        return response

    return await _drive_plan_loop(ctx, response, session_id)


plan_executor_workflow = Workflow(
    name="ori_plan_executor",
    description=(
        "Native ADK 2.0 plan-and-execute via a deterministic dynamic "
        "workflow. Single-turn chat passes through one coordinator call. "
        "Multi-step plans are driven by a code-side step loop: "
        "get_next_step / coordinator turn / complete_step, repeated until "
        "plan_storage reports no pending steps. The LLM cannot skip steps, "
        "reorder them, or escape the loop early."
    ),
    edges=[
        ("START", plan_executor_node),
    ],
)
