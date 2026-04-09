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
from app.callbacks.guardrails import prompt_injection_guardrail
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


def _get_company_domain() -> str:
    return os.environ.get("COMPANY_DOMAIN", "").strip().lower()


def before_clickup_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Domain-level access control — only @COMPANY_DOMAIN users can use ClickUp tools."""
    company_domain = _get_company_domain()
    if not company_domain:
        return None
    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    user_email = state.get("user_id", "")
    if not user_email or not user_email.lower().endswith(f"@{company_domain}"):
        return {
            "error": f"Access denied: ClickUp is only available to @{company_domain} users. "
                     f"Your identity ({user_email or 'unknown'}) is not authorized."
        }
    return None


if os.environ.get("CLICKUP_API_TOKEN", "").strip():
    clickup_agent = Agent(
        name="ClickUpAgent",
        model=get_model("CoordinatorAgent"),
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
        before_model_callback=prompt_injection_guardrail,
        before_tool_callback=before_clickup_callback,
    )
else:
    clickup_agent = None
    logger.info("ClickUpAgent disabled: CLICKUP_API_TOKEN not configured")
