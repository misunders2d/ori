"""Plan-and-execute Workflow — coordinator agent + completion-check loop.

ADK 2.0 native: a single Workflow graph with the coordinator as a node
and a `plan_completion_check` function-node that loops back via a routed
edge while the active plan has pending steps.

  START -> coordinator -> plan_completion_check
    if pending steps:  Event(route="continue")  -> back to coordinator
    if all done:       None                     -> workflow terminates

This is the production root agent. Single path — no env-flag fallback.
For sessions without a plan, the check immediately returns None and the
workflow terminates after one coordinator turn (single-turn chat works
exactly like before).

Crash mid-plan: state is durable in `data/plans.db` (Phase B). On restart,
the coordinator's next get_next_step picks up the in-progress step
without double-claiming.
"""

from __future__ import annotations

import logging

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import Workflow, node

from app.agents.coordinator import root_agent as coordinator_agent
from app.runtime.plan_storage import has_pending_steps
from app.state import OriSessionState

logger = logging.getLogger(__name__)


# Continuation prompt — fed as the coordinator's next input on each loop.
# Multilingual-safe: uses no English-keyword triggers; just a system-level
# instruction the LLM follows in any language.
_CONTINUATION_PROMPT = (
    "Your enforced plan still has pending steps. Do NOT emit a user-facing "
    "summary yet. Call get_next_step immediately and continue executing. "
    "Only produce a final summary after complete_step reports "
    "'All steps completed!'."
)

_MAX_ITERATIONS = 25
_ITER_KEY = "plan_workflow_iters"


@node(name="plan_completion_check", rerun_on_resume=False)
async def plan_completion_check(ctx: Context, node_input):
    """Inspect plan state. Loop back to coordinator while steps remain.

    Returns None on terminate (no active plan or all done) — that emits no
    downstream event so the workflow ends with the coordinator's last
    response as the final output.
    """
    sess = getattr(ctx, "session", None)
    session_id = getattr(sess, "id", None) or getattr(sess, "session_id", None) if sess else None
    if not session_id:
        return None

    iter_count = ctx.state.get(_ITER_KEY, 0) + 1
    ctx.state[_ITER_KEY] = iter_count
    if iter_count > _MAX_ITERATIONS:
        logger.warning(
            "plan_executor: iteration cap %d hit for session %s",
            _MAX_ITERATIONS, session_id,
        )
        return None

    if await has_pending_steps(session_id):
        return Event(output=_CONTINUATION_PROMPT, route="continue")
    return None


plan_executor_workflow = Workflow(
    name="ori_plan_executor",
    description=(
        "Native ADK 2.0 plan-and-execute. Single-turn chat passes through "
        "(check returns None, terminates). Multi-step plans loop the "
        "coordinator until plan_storage.has_pending_steps is False."
    ),
    state_schema=OriSessionState,
    edges=[
        ("START", coordinator_agent),
        (coordinator_agent, plan_completion_check),
        # Loop back when the check returns Event(route="continue").
        # Cycles in ADK 2.0 require at least one routed edge — this satisfies it.
        (plan_completion_check, {"continue": coordinator_agent}),
    ],
)
