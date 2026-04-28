"""SubAgentPopperPlugin — emit end_of_agent on sub-agent completion.

ADK 2.0 chat-mode sub-agents do NOT auto-pop back to the parent after
`transfer_to_agent`. The runner's `_find_agent_to_run` walks reversed
session events and picks the most-recent transferable agent — which
remains the sub-agent across subsequent turns until something resets
the active agent. Per the ADK 2.0 collaboration docs, only `task` and
`single_turn` mode agents auto-return; chat mode requires manual
transfer back, which the LLM doesn't reliably do.

This plugin hooks `after_agent_callback` for any non-root agent and
sets `callback_context.actions.end_of_agent = True`, signaling that
the sub-agent's invocation has completed. ADK's `_event_filter`
(in `runners.py:_find_agent_to_run`) skips events with
`end_of_agent=True`, so the resume walk continues past the sub-agent's
events and lands on the coordinator's earlier event — restoring the
parent as the active agent for the next turn.

This is the proper ADK 2.0 lifecycle fix for the "sub-agent remains
active after delegation" routing bug. Replaces the earlier
`_stamp_coordinator_active` marker workaround in `app/tasks.py` (kept
for now as belt-and-suspenders; can be removed once this is verified).
"""

from __future__ import annotations

import logging

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin
from google.genai import types

logger = logging.getLogger(__name__)


class SubAgentPopperPlugin(BasePlugin):
    """Emit end_of_agent on sub-agent completion to pop back to parent."""

    def __init__(self) -> None:
        super().__init__(name="sub_agent_popper")

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        # Only sub-agents pop. The root coordinator stays active.
        if getattr(agent, "parent_agent", None) is None:
            return None
        try:
            callback_context.actions.end_of_agent = True
            logger.debug(
                "sub_agent_popper: marked end_of_agent=True for %s",
                agent.name,
            )
        except Exception as e:
            # If the actions object is somehow not mutable / not present,
            # we don't want to crash the agent invocation. The marker
            # fallback in tasks.py still applies.
            logger.warning(
                "sub_agent_popper: failed to set end_of_agent for %s: %s",
                agent.name, e,
            )
        return None
