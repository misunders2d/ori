"""ClickUp task management sub-agent.

Conditionally enabled when CLICKUP_API_TOKEN is configured.
Access gated to users whose email matches COMPANY_DOMAIN.
"""

import logging
import os
import pathlib
from typing import Any

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app.app_utils.models import get_model
from app.callbacks.guardrails import (
    force_bounce_before_model,
    on_tool_error_bouncer,
    prompt_injection_guardrail,
    reset_error_history_after_tool,
    tool_output_spillover_guardrail,
)
from app.toolsets import ScratchpadToolset
from app.toolsets.clickup import ClickUpToolset

logger = logging.getLogger(__name__)

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_clickup_skill = load_skill_from_dir(_base_dir / "clickup-skill")
_scratchpad_skill = load_skill_from_dir(_base_dir / "scratchpad-skill")

# Load instructions from SKILL.md
_SKILL_MD = _base_dir / "clickup-skill" / "SKILL.md"
try:
    _raw = _SKILL_MD.read_text()
    if _raw.startswith("---"):
        parts = _raw.split("---", 2)
        _instruction = parts[2].strip() if len(parts) >= 3 else _raw
    else:
        _instruction = _raw
except FileNotFoundError:
    _instruction = "You are a ClickUp task management agent. Help users manage their tasks."

# Universal routing fallback — appended to whatever the skill provides
# so the agent can bounce out-of-domain requests instead of refusing.
# ClickUpAgent is a direct child of CoordinatorAgent, so it bounces up
# to the coordinator (not via AmazonHeadAgent).
_instruction += (
    "\n\nROUTING FALLBACK: If the user's request is outside ClickUp task "
    "management (e.g. Amazon product research, BigQuery SQL, charts, "
    "Drive/Sheets, decks, code, A2A), call "
    "`transfer_to_agent(agent_name='CoordinatorAgent')` so the coordinator "
    "can re-route. Do not refuse, guess, or answer outside your domain. System / lifecycle requests (reboot, restart, rollback, update, shut down) are ALWAYS outside your domain — bounce immediately, do not invent a refusal."
)


def _get_company_domain() -> str:
    return os.environ.get("COMPANY_DOMAIN", "").strip().lower()


def _get_admin_ids() -> list[str]:
    return [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]


def before_clickup_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Domain-level access control — only @COMPANY_DOMAIN users can use ClickUp tools.

    Scoped to actual ClickUp tools (names starting with ``clickup_``). This
    callback is registered as the agent-level ``before_tool_callback``, so it
    also fires for cross-cutting tools (planner, scratchpad, skill lookup)
    that a scheduled task or coordinator-delegated flow might invoke in this
    agent's context — those should pass through unconditionally.

    Admins bypass the company-domain check entirely: scheduled tasks and
    admin-driven operations shouldn't get blocked because the admin happens
    to be signed in via a Telegram ID instead of a work email.
    """
    tool_name = getattr(tool, "name", "") or ""
    if not tool_name.startswith("clickup_"):
        return None  # Not a ClickUp tool → not gated by this callback.

    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    user_id = state.get("user_id", "") or ""

    # Admin bypass — admins are trusted regardless of domain match.
    if user_id in _get_admin_ids():
        return None

    company_domain = _get_company_domain()
    if not company_domain:
        return None

    if not user_id.lower().endswith(f"@{company_domain}"):
        return {
            "error": f"Access denied: ClickUp is only available to @{company_domain} users. "
                     f"Your identity ({user_id or 'unknown'}) is not authorized."
        }
    return None


if os.environ.get("CLICKUP_API_TOKEN", "").strip():
    clickup_agent = Agent(
        name="ClickUpAgent",
        model=get_model("ClickUpAgent"),
        description=(
            "Task management agent for ClickUp. Handles creating, updating, assigning, "
            "and querying tasks. Use for any ClickUp-related requests including team "
            "coordination, task assignments, and project tracking."
        ),
        instruction=_instruction,
        tools=[
            skill_toolset.SkillToolset(skills=[_clickup_skill, _scratchpad_skill]),
            ClickUpToolset(),
            ScratchpadToolset(),
        ],
        before_model_callback=[force_bounce_before_model, prompt_injection_guardrail],
        after_tool_callback=[tool_output_spillover_guardrail, reset_error_history_after_tool],
        on_tool_error_callback=on_tool_error_bouncer,
        before_tool_callback=before_clickup_callback,
    )
else:
    clickup_agent = None
    logger.info("ClickUpAgent disabled: CLICKUP_API_TOKEN not configured")
