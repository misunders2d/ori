"""ClickUp task management sub-agent.

Conditionally enabled when CLICKUP_API_TOKEN is configured.
Access gated to users whose email matches COMPANY_DOMAIN; admins bypass.
The gate runs as a `before_tool_callback` (still supported in ADK 2.0 for
agent-scoped tool gating; App-level concerns live in plugins).
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app.toolsets import ScratchpadToolset
from app.toolsets.clickup import ClickUpToolset
from app.util.models import get_model

logger = logging.getLogger(__name__)

_SKILLS_DIR = pathlib.Path(__file__).parent.parent.parent / "skills"
_clickup_skill = load_skill_from_dir(_SKILLS_DIR / "clickup-skill")
_scratchpad_skill = load_skill_from_dir(_SKILLS_DIR / "scratchpad-skill")

# Lift instruction from SKILL.md frontmatter so the skill is the single source.
_SKILL_MD = _SKILLS_DIR / "clickup-skill" / "SKILL.md"
try:
    _raw = _SKILL_MD.read_text()
    if _raw.startswith("---"):
        parts = _raw.split("---", 2)
        _instruction = parts[2].strip() if len(parts) >= 3 else _raw
    else:
        _instruction = _raw
except FileNotFoundError:
    _instruction = "You are a ClickUp task management agent. Help users manage their tasks."


def _company_domain() -> str:
    return os.environ.get("COMPANY_DOMAIN", "").strip().lower()


def _admin_ids() -> list[str]:
    return [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]


def before_clickup_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Domain-level access control — only @COMPANY_DOMAIN users can use ClickUp tools.

    Scoped to actual ClickUp tools (names starting with `clickup_`). This callback
    is registered as the agent-level `before_tool_callback`, so it also fires for
    cross-cutting tools (planner, scratchpad, skill lookup) that a scheduled task
    or coordinator-delegated flow might invoke in this agent's context — those
    pass through unconditionally.

    Admins bypass the company-domain check entirely.
    """
    tool_name = getattr(tool, "name", "") or ""
    if not tool_name.startswith("clickup_"):
        return None

    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    user_id = state.get("user_id", "") or ""

    if user_id in _admin_ids():
        return None

    domain = _company_domain()
    if not domain:
        return None

    if not user_id.lower().endswith(f"@{domain}"):
        return {
            "error": (
                f"Access denied: ClickUp is only available to @{domain} users. "
                f"Your identity ({user_id or 'unknown'}) is not authorized."
            )
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
        before_tool_callback=before_clickup_callback,
    )
else:
    clickup_agent = None
    logger.info("ClickUpAgent disabled: CLICKUP_API_TOKEN not configured")
