"""Plan enforcement: soft injection + hard tool-block.

Two layers work together when a plan is active for the session:

- `plan_enforcer` (before_model_callback): injects the active plan
  context into the LLM prompt so the model sees what step it's on
  and what it's supposed to do. Soft fence — guidance, not block.
- `plan_step_enforcer` (before_tool_callback): hard fence that
  rejects any tool call outside the current step's `allowed_tools`
  whitelist. Layered on top of the soft fence to catch deviations
  the LLM tries anyway.

A small set of tools is always allowed (`_PLAN_EXEMPT_TOOLS`) — plan
lifecycle tools, working-memory tools, and ADK delegation primitives.
Without this allow-list a hard-enforced plan would deadlock.
"""

import fnmatch
import logging

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

logger = logging.getLogger(__name__)


def plan_enforcer(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
    """Injects active plan context into the model prompt to enforce step-by-step execution."""
    from app.tools.planner import get_active_plan_context

    session = (
        getattr(callback_context, "session", None)
        if hasattr(callback_context, "session")
        else None
    )
    if not session:
        return None
    session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
    if not session_id:
        return None

    context = get_active_plan_context(session_id)
    if context and llm_request.contents:
        llm_request.append_instructions([context])

    return None


# Tools that are always allowed regardless of the active step's
# allowed_tools list — they manage the plan itself, control transport
# (the agent must be able to ask the user for confirmation / acknowledge
# completion), or are inherent to ADK's delegation primitives. Without
# this allow-list a hard-enforced plan would deadlock the agent.
_PLAN_EXEMPT_TOOLS = frozenset(
    {
        # Plan lifecycle
        "create_plan",
        "get_next_step",
        "complete_step",
        "get_plan_status",
        "abandon_plan",
        # Working memory the agent always needs access to
        "scratchpad_read",
        "scratchpad_write",
        "scratchpad_list",
        "scratchpad_replace",
        "scratchpad_clear",
        # ADK primitives + reflection
        "transfer_to_agent",
    }
)


def _glob_match(name: str, patterns: list[str]) -> bool:
    """True if `name` matches any glob pattern (fnmatch syntax)."""
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def plan_step_enforcer(tool, args, tool_context, **kwargs) -> dict | None:
    """before_tool guard: block tool calls outside the active step's allowed_tools.

    When the current step has an `allowed_tools` whitelist, only tool
    names that match one of those globs (or are in `_PLAN_EXEMPT_TOOLS`)
    are permitted through. Everything else returns a synthetic error
    response that tells the LLM what's allowed for this step, prompting
    it to either complete the step or abandon the plan.

    No active plan / no constraints / no in-progress step → pass through.
    This is layered on top of `plan_enforcer` (which still injects the
    prompt context) — the soft fence stays as guidance for the LLM, the
    hard fence here catches deviations the LLM tries anyway.
    """
    if not tool or not tool_context:
        return None

    tool_name = getattr(tool, "name", "") or ""
    if tool_name in _PLAN_EXEMPT_TOOLS:
        return None

    session = getattr(tool_context, "session", None)
    session_id = (
        getattr(session, "session_id", None)
        or getattr(session, "id", None)
        if session is not None
        else None
    )
    if not session_id:
        return None

    try:
        from app.tools.planner import get_current_step_constraints
        constraints = get_current_step_constraints(session_id)
    except Exception as e:
        logger.warning("plan_step_enforcer: planner unreachable (%s) — pass-through", e)
        return None

    if not constraints:
        return None  # no plan / no in-progress step / unconstrained step

    allowed = constraints.get("allowed_tools") or []
    if not allowed:
        return None  # only must_call set — let it through

    if _glob_match(tool_name, allowed):
        return None

    return {
        "status": "error",
        "message": (
            f"Plan-step guardrail: tool `{tool_name}` is not allowed for step "
            f"{constraints['step_id']} ({constraints['description']!r}). "
            f"Allowed for this step: {allowed}. "
            "Complete the current step (`complete_step`) or abandon the plan (`abandon_plan`) "
            "before calling other tools."
        ),
    }
