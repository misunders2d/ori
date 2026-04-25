"""PlanEnforcerPlugin — inject active plan context into the LLM prompt.

When a session has an active plan in `app/runtime/plan_storage.py`, this
plugin prepends a short context block to every LLM call so the agent
cannot drift away from the current step. Works hand-in-hand with the
scheduler's `seed_plan(...)` (which seeds before turn one) and the
agent-facing tools `create_plan` / `get_next_step` / `complete_step`.

Scoped: fires only on the root agent. Sub-agents see the parent's
contents already (the workflow framework forwards them); injecting again
would double-up the directive.
"""

from __future__ import annotations

import logging

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin
from google.genai import types

from app.plugins._common import state_session_id
from app.runtime.plan_storage import get_active_plan_context

logger = logging.getLogger(__name__)


class PlanEnforcerPlugin(BasePlugin):
    """Prepends active-plan context to the LLM call, on the root agent."""

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
        if llm_request.contents:
            llm_request.contents.insert(0, types.Content(
                role="user",
                parts=[types.Part.from_text(text=context)],
            ))
        return None
