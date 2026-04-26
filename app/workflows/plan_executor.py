"""Plan-and-execute workflow — dynamic-workflow pattern.

ADK 2.0 idiom per the official docs (https://adk.dev/workflows/dynamic/):
the workflow IS a single `@node`-decorated async function that runs the
coordinator and re-runs it inside a `while` loop until the plan has no
pending steps. All control flow lives in Python — cancellation
propagates naturally through `asyncio.CancelledError` at await points,
so a `task.cancel()` from the transport layer cleanly aborts a
mid-flight loop without re-firing the coordinator.

This replaces the earlier graph-edge approach which had two failure
modes:
1. Graph edges re-routed back to the coordinator AFTER `task.cancel()`
   killed the runner task, generating extra tool calls (live-reproduced
   bug: "create one image" → 3 images, even after user said stop).
2. The state-keyed iteration counter persisted across user turns,
   making the cap behave unpredictably across long sessions.

Single-turn chat passes through one coordinator call. Multi-step plans
loop until `runtime.plan_storage.has_pending_steps` is False.
"""

from __future__ import annotations

import logging

from google.adk.agents.context import Context
from google.adk.workflow import Workflow, node

from app.agents.coordinator import root_agent as coordinator_agent
from app.runtime.plan_storage import has_pending_steps

logger = logging.getLogger(__name__)


_CONTINUATION_PROMPT = (
    "Your enforced plan still has pending steps. Do NOT emit a user-facing "
    "summary yet. Call get_next_step immediately and continue executing. "
    "Only produce a final summary after complete_step reports "
    "'All steps completed!'."
)

_MAX_ITERATIONS = 25


@node(name="plan_executor", rerun_on_resume=True)
async def plan_executor_node(ctx: Context, node_input):
    """Run the coordinator once, then loop while a plan is pending.

    For single-turn chat (no plan, or no pending steps) the loop body is
    never entered — equivalent to running the coordinator agent directly,
    just wrapped in workflow context for resumability + checkpointing.
    """
    response = await ctx.run_node(coordinator_agent, node_input)

    session = getattr(ctx, "session", None)
    session_id = (
        getattr(session, "id", None) or getattr(session, "session_id", None)
        if session
        else None
    )
    if not session_id:
        return response

    iterations = 0
    while await has_pending_steps(session_id) and iterations < _MAX_ITERATIONS:
        iterations += 1
        response = await ctx.run_node(coordinator_agent, _CONTINUATION_PROMPT)

    if iterations >= _MAX_ITERATIONS and await has_pending_steps(session_id):
        logger.warning(
            "plan_executor: hit iteration cap %d for session %s",
            _MAX_ITERATIONS, session_id,
        )

    return response


plan_executor_workflow = Workflow(
    name="ori_plan_executor",
    description=(
        "Native ADK 2.0 plan-and-execute via dynamic workflow. Single-turn "
        "chat passes through one coordinator call. Multi-step plans loop "
        "until plan_storage.has_pending_steps is False. Cancellation "
        "propagates through Python await semantics — task.cancel() cleanly "
        "aborts the loop without re-firing."
    ),
    edges=[
        ("START", plan_executor_node),
    ],
)
