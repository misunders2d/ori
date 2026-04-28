"""PlanEnforcerPlugin — inject active plan context as a system instruction.

When a session has an active plan in `app/runtime/plan_storage.py`, this
plugin appends a short directive block to the LLM call's
`config.system_instruction` so the agent cannot drift away from the
current step. Works hand-in-hand with the scheduler's `seed_plan(...)`
(which seeds before turn one) and the agent-facing tools
`create_plan` / `get_next_step` / `complete_step`.

Scoped: fires only on the root coordinator (agents with no parent_agent).
Sub-agents reached via `transfer_to_agent` skip injection AND — because
system_instruction lives on the per-call request config rather than in
`llm_request.contents` — the directive does not bleed into history that
those sub-agents inherit. That isolates `complete_step` / `get_next_step`
to the agent that actually has those tools (the coordinator), preventing
sub-agents from blindly attempting planner calls they can't fulfill.

Reference: ADK 2.0 `LlmRequest.append_instructions(...)` writes to
`llm_request.config.system_instruction`. system_instruction is per-call
and not persisted as a session event, so child agents in the same session
do NOT see this directive. Verified at
https://adk.dev/workflows/collaboration/.
"""

from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin

from app.plugins._common import state_session_id
from app.runtime.plan_storage import get_active_plan_context

logger = logging.getLogger(__name__)


class PlanEnforcerPlugin(BasePlugin):
    """Appends active-plan directive to system_instruction on the root agent."""

    def __init__(self) -> None:
        super().__init__(name="plan_enforcer")

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        # Only the root agent injects plan context.
        agent = getattr(callback_context, "agent", None)
        if agent is not None and getattr(agent, "parent_agent", None) is not None:
            return None

        session_id = state_session_id(callback_context)
        if not session_id:
            return None

        context = await get_active_plan_context(session_id)
        if not context:
            return None
        # System instruction → per-call, not persisted, not forwarded to
        # transferred sub-agents. The directive references planner tools
        # that ONLY the root coordinator has.
        llm_request.append_instructions([context])
        return None
