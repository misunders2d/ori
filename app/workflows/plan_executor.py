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
from app.runtime.plan_storage import (
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
    """The exact prompt the coordinator sees for one step.

    Hard rule embedded in the prompt: do NOT call planner mechanics
    (`get_next_step`, `complete_step`, `abandon_plan`). The workflow
    records completion in code; the coordinator's only job is to do
    the step's work and return the result. Mentioning planner tools
    in the prompt would invite the LLM to call them; not mentioning
    them keeps focus on the step.
    """
    return (
        f"PLAN STEP {step['step_index']}: {step['description']}\n\n"
        "Execute this step now. Delegate to a sub-agent if appropriate. "
        "Reply with the result/output of completing this step. "
        "Do NOT discuss the next step; the workflow advances automatically."
    )


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

    iterations = 0
    while (
        await has_pending_steps(session_id)
        and iterations < _MAX_ITERATIONS
    ):
        iterations += 1

        # CODE-SIDE step claim. The LLM never sees the full plan, never
        # picks which step is next, never reorders.
        step = await get_next_step(session_id)
        if not step:
            break

        # Build a clean per-step prompt and run the coordinator on it.
        # transfer_to_agent inside this turn dispatches to sub-agents as
        # the coordinator's instruction sees fit (Amazon, Knowledge, etc.).
        step_response = await ctx.run_node(
            coordinator_agent, _build_step_prompt(step),
        )

        # CODE-SIDE completion record. Whatever the coordinator returned
        # becomes the step's result. The LLM cannot skip recording.
        result_text = _extract_text(step_response)
        await complete_step(session_id, result_text[:_RESULT_RECORD_LIMIT])
        response = step_response

    if iterations >= _MAX_ITERATIONS and await has_pending_steps(session_id):
        logger.warning(
            "plan_executor: hit iteration cap %d for session %s",
            _MAX_ITERATIONS, session_id,
        )

    return response


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
