"""Perimeter ACL plugin — the outermost guardrail.

Denies invocations from users not in the whitelist (or in the blacklist).
Defense-in-depth against transport-level oversights: the Telegram/Slack
pollers also check perimeter before invoking the runner, but if any
transport ever forgets, this plugin still catches it.

Order: First in App(plugins=[...]) so denied calls short-circuit before
state initialization or model setup runs.

A2A callers are trusted automatically (the A2A executor validated the
API key before the runner was invoked).
"""

from __future__ import annotations

import logging

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin
from google.genai import types

from app.plugins._common import is_a2a_user, state_user_id
from app.runtime import perimeter

logger = logging.getLogger(__name__)


class PerimeterAclPlugin(BasePlugin):
    """Whitelist/blacklist enforcement at agent invocation time."""

    def __init__(self) -> None:
        super().__init__(name="perimeter")

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        # Only fire on the root agent — sub-agent delegations within an
        # already-allowed session are safe.
        if getattr(agent, "parent_agent", None) is not None:
            return None

        user_id = state_user_id(callback_context)
        if is_a2a_user(user_id):
            return None  # A2A keys are pre-validated

        if perimeter.is_blacklisted(user_id):
            logger.warning("PerimeterAclPlugin: BLOCKED blacklisted user %s", user_id)
            return _denied(user_id, "blacklisted")

        if not perimeter.is_allowed(user_id):
            logger.info("PerimeterAclPlugin: REJECTED non-whitelisted user %s", user_id)
            return _denied(user_id, "not authorized")

        return None


def _denied(user_id: str, reason: str) -> types.Content:
    """Language-agnostic short token + structured info — the LLM never sees
    this (we short-circuit), but downstream loggers / transport layers can
    react to the consistent format. Returned via a Content because that's
    the BasePlugin.before_agent_callback signature.
    """
    return types.Content(
        parts=[
            types.Part(
                text=(
                    "[PERIMETER_DENIED] reason="
                    + reason
                    + f" user_id={user_id}"
                )
            )
        ]
    )
