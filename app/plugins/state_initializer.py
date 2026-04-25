"""StateInitializerPlugin — bootstrap session state on the first agent turn.

Idempotent: only sets keys that don't yet exist. Fires only on the root
agent (sub-agent delegations inherit state). Loads:
- user_id (if not already set; defaults to callback_context.user_id)
- master_user_id (admin list from env)
- bot_name (env-controlled)
- user_preferences (from disk via app.tools.preferences — best-effort)
"""

from __future__ import annotations

import logging
import os

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin
from google.genai import types

from app.plugins._common import admin_user_ids

logger = logging.getLogger(__name__)


class StateInitializerPlugin(BasePlugin):
    """Run-once session-state setup. Runs on every agent invocation but
    only writes keys that aren't present (so downstream turns are no-ops)."""

    def __init__(self) -> None:
        super().__init__(name="state_initializer")

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        # Only run on the root agent — descendants inherit the root's state.
        if getattr(agent, "parent_agent", None) is not None:
            return None

        state = callback_context.state
        current = state.to_dict() if state else {}

        if "user_id" not in current:
            state["user_id"] = getattr(callback_context, "user_id", "") or ""
        if "master_user_id" not in current:
            state["master_user_id"] = admin_user_ids()
        # bot_name re-reads env every turn (cheap, supports rename across restarts).
        state["bot_name"] = os.environ.get("BOT_NAME", "Ori")

        # User preferences: best-effort. The tools layer has the disk loader;
        # we don't fail the turn if it errors.
        try:
            from app.tools.preferences import load_user_preferences
            effective_user = state.to_dict().get("user_id", "") if state else ""
            if effective_user:
                state["user_preferences"] = load_user_preferences(effective_user)
        except Exception as e:
            logger.debug("StateInitializerPlugin: preferences load skipped: %s", e)

        return None
