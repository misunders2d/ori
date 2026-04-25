"""VerifyRetryPlugin — 3-strike cap on evolution_verify_sandbox failures.

When evolution_verify_sandbox fails three times consecutively in a session,
this plugin returns a hard error directing the agent to stop attempting
fixes and report the issue. Counter resets on success.

Detection is dict-result based (not exception-based) — `tool_response`
returns `{"status": "error", ...}` on failure. ReflectAndRetryToolPlugin
from ADK 2.0's stdlib handles exception-based retry but doesn't fit our
soft-failure shape, so we implement directly.

Scoped to DeveloperAgent (the only agent that calls evolution_verify_sandbox).
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


_TARGET_TOOL = "evolution_verify_sandbox"
_MAX_FAILURES = 3
# State key — also declared on OriSessionState for type safety.
_COUNTER_KEY = "verify_failure_count"


class VerifyRetryPlugin(BasePlugin):
    """Hard cap on consecutive verify failures."""

    def __init__(self) -> None:
        super().__init__(name="verify_retry")

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        if tool.name != _TARGET_TOOL:
            return None

        is_failure = isinstance(result, dict) and result.get("status") == "error"
        state = tool_context.state

        if not is_failure:
            state[_COUNTER_KEY] = 0
            return None

        current = (state.to_dict() if state else {}).get(_COUNTER_KEY, 0) + 1
        state[_COUNTER_KEY] = current

        if current >= _MAX_FAILURES:
            logger.warning(
                "VerifyRetryPlugin: hit retry limit (%d/%d) — halting attempts",
                current, _MAX_FAILURES,
            )
            return {
                "status": "error",
                "error_code": "VERIFY_RETRY_LIMIT",
                "message": (
                    f"RETRY LIMIT REACHED: Verification has failed {current} "
                    f"consecutive times. You MUST stop attempting fixes. Report "
                    f"the issue back to the user with: (1) what you were trying "
                    f"to do, (2) the error output, and (3) what you found during "
                    f"your research. Do NOT call evolution_verify_sandbox again."
                ),
            }

        # Inject a research nudge into the existing response so the LLM sees
        # both the original error AND the directive to research before retry.
        remaining = _MAX_FAILURES - current
        if isinstance(result, dict):
            result["retry_warning"] = (
                f"Verification failed ({current}/{_MAX_FAILURES} attempts used, "
                f"{remaining} remaining). You MUST research the error externally "
                f"before retrying — use search_github_issues, "
                f"check_installed_package, or google_search_agent_tool."
            )
        return None
