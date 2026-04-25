"""StateInitializerPlugin — bootstrap session state on every user turn.

Empirically determined hook choice:
ADK 2.0 does NOT fire `before_agent_callback` for LlmAgents that run as
Workflow nodes — `run_llm_agent_as_node` bypasses BaseAgent.run_async's
`_handle_before_agent_callback`. So state seeded in before_agent_callback
is invisible to the Coordinator's instruction template substitution, and
`{bot_name}` KeyErrors at boot for inbound A2A and Telegram alike.

`on_user_message_callback` DOES fire on the workflow path (verified with
a probe plugin against the real App + InMemoryRunner). It runs once per
user turn before the workflow executes — early enough to seed state for
instruction substitution.

Sets:
- user_id (if not already set)
- master_user_id (admin list from env)
- bot_name (env-controlled, written every turn)
- user_preferences (best-effort disk load, scoped by user_id)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins import BasePlugin
from google.genai import types

from app.plugins._common import admin_user_ids

logger = logging.getLogger(__name__)


class StateInitializerPlugin(BasePlugin):
    """Per-turn session-state setup. Runs at message receipt; idempotent
    on user_id/master_user_id; bot_name is a fresh env read every turn
    (supports renames across restarts)."""

    def __init__(self) -> None:
        super().__init__(name="state_initializer")

    async def on_user_message_callback(
        self,
        *,
        invocation_context: InvocationContext,
        user_message: types.Content,
    ) -> types.Content | None:
        sess = getattr(invocation_context, "session", None)
        state: dict[str, Any] | None = getattr(sess, "state", None) if sess else None
        if state is None:
            return None

        if "user_id" not in state:
            uid = getattr(invocation_context, "user_id", "") or ""
            if not uid and sess is not None:
                uid = getattr(sess, "user_id", "") or ""
            state["user_id"] = uid
        if "master_user_id" not in state:
            state["master_user_id"] = admin_user_ids()
        # bot_name re-reads env every turn (cheap, supports rename across restarts).
        state["bot_name"] = os.environ.get("BOT_NAME", "Ori")

        # User preferences: best-effort. The tools layer has the disk loader;
        # we don't fail the turn if it errors.
        try:
            from app.tools.preferences import load_user_preferences
            effective_user = state.get("user_id", "")
            if effective_user:
                state["user_preferences"] = load_user_preferences(effective_user)
        except Exception as e:
            logger.debug("StateInitializerPlugin: preferences load skipped: %s", e)

        return None
