"""Admin gating: blocks privileged tools for non-admins; stages admin
intent via ACT-XXXXXX tokens for explicit approval.

Two responsibilities:
1. before_agent_callback — denies the entire DeveloperAgent for non-admins
   (developer tools collectively are admin-only).
2. before_tool_callback — for a specific set of privileged tools, stages
   the intent via app.runtime.pending_actions and asks the user to reply
   with `Approve ACT-XXXXXX`. Optionally requires TOTP.

A2A callers (validated API key) are trusted on tool calls but cannot
invoke the developer agent unless their user_id is also in ADMIN_USER_IDS.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.plugins._common import (
    admin_user_ids,
    env_truthy,
    is_a2a_user,
    state_user_id,
)
from app.runtime.pending_actions import stage_action

logger = logging.getLogger(__name__)

# Privileged tools that require ACT-XXXXXX approval.
_GATED_TOOLS: frozenset[str] = frozenset({
    "configure_integration",
    "remove_integration",
    "schedule_system_task",
    "schedule_recurring_system_task",
    "run_system_task_now",
    "update_self",
    "trigger_rollback",
    "evolution_commit_and_push",
})

# Agents whose entire invocation is admin-only.
_ADMIN_ONLY_AGENTS: frozenset[str] = frozenset({"DeveloperAgent"})


class AdminGatePlugin(BasePlugin):
    """Admin role gating — agent-level + tool-level ACT staging."""

    def __init__(self) -> None:
        super().__init__(name="admin_gate")

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        if agent.name not in _ADMIN_ONLY_AGENTS:
            return None
        user_id = state_user_id(callback_context)
        admins = admin_user_ids()
        if not admins:
            return types.Content(parts=[types.Part(text=(
                "[ADMIN_NOT_CONFIGURED] ADMIN_USER_IDS is empty. "
                f"Found user_id={user_id}; configure that as admin to proceed."
            ))])
        if is_a2a_user(user_id):
            return None  # validated key
        if user_id not in admins:
            return types.Content(parts=[types.Part(text=(
                "[ADMIN_REQUIRED] agent=" + agent.name + f" user_id={user_id}"
            ))])
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        if tool.name not in _GATED_TOOLS:
            return None

        user_id = tool_context.state.to_dict().get("user_id", "") if tool_context.state else ""
        admins = admin_user_ids()

        # Non-admin: block immediately.
        if not is_a2a_user(user_id) and (not admins or user_id not in admins):
            return {
                "status": "error",
                "error_code": "ADMIN_REQUIRED",
                "message": (
                    f"Only admin users can invoke `{tool.name}`. "
                    f"Your user_id ({user_id}) is unauthorized."
                ),
            }

        # Admin: stage the intent. Caller must reply with `Approve ACT-XXXXXX`
        # which `execute_approved_action` consumes.
        try:
            session_id = tool_context.state.to_dict().get("session_id", "default") if tool_context.state else "default"
            token = stage_action(tool.name, tool_args, user_id, session_id)
            logger.info("AdminGatePlugin: staged %s for %s -> %s", tool.name, user_id, token)
        except Exception as e:
            logger.error("AdminGatePlugin: failed to stage action: %s", e)
            return {
                "status": "error",
                "error_code": "STAGE_FAILED",
                "message": "Failed to stage your action for approval. Check logs.",
            }

        with_2fa = bool(os.environ.get("ADMIN_TOTP_SECRET")) and env_truthy("REQUIRE_2FA", default=True)
        if with_2fa:
            instruction = f"`Approve {token} <your-6-digit-code>`"
        else:
            instruction = f"`Approve {token}`"
        return {
            "status": "error",
            "error_code": "ACTION_STAGED",
            "act_token": token,
            "message": (
                f"**CRITICAL ACTION STAGED**\n\n"
                f"To protect the system, the `{tool.name}` command requires explicit admin confirmation.\n\n"
                f"Please reply with:\n{instruction}\n\n"
                f"_Note: This token expires in 15 minutes and is single-use._"
            ),
        }
