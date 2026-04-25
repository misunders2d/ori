"""CoordinatorAgent — root LLM-routed orchestrator.

Delegates to DeveloperAgent (for self-evolution) and KnowledgeAgent (for
A2A and DNA exchange). Handles everything else directly: research,
scheduling, memory, perimeter management, plan-and-execute.

LLM-discretion routing via `transfer_to_agent` — multilingual-safe by
construction (the LLM sees the user's intent in their language and picks
the right sub-agent without keyword matching).

No callback kwargs — guardrails attach at the App level (see app/agent.py).
Model is hot-swappable via state.model['CoordinatorAgent'] (LiteLlm-routed,
defaults to gemini/gemini-2.5-flash).
"""

from __future__ import annotations

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.agents.developer import developer_agent
from app.agents.knowledge import knowledge_agent
from app.toolsets import (
    MemoryToolset,
    PlannerToolset,
    SchedulingToolset,
    SystemToolset,
)
from app.tools.google_search import google_search_agent_tool
from app.tools.web import web_fetch
from app.tools.whitelist import blacklist_chat, whitelist_chat
from app.util.models import get_model


_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_scheduling_skill = load_skill_from_dir(_skills_dir / "scheduling-skill")


_INSTRUCTION = (
    "You are {bot_name}, an autonomous self-evolving agent platform. "
    "Your job is to orchestrate tasks, remember context, and delegate "
    "specialized work.\n\n"

    "DELEGATION:\n"
    "1. Self-evolution (code changes, bug fixes, adding features, OAuth "
    "integration setup): delegate to DeveloperAgent.\n"
    "2. A2A communication, friend management, DNA exchange: delegate to "
    "KnowledgeAgent.\n"
    "3. Everything else (research, scheduling, memory, perimeter, plans, "
    "small text answers): handle directly.\n\n"

    "EAGER DELEGATION RULE: answer questions directly first. Delegate to "
    "DeveloperAgent ONLY on explicit action requests ('fix it', 'write the "
    "code', 'integrate X'). Knowledge questions about A2A or friends do "
    "NOT require KnowledgeAgent — answer from your own context.\n\n"

    "SPAWNING: You can spawn child agents (`spawn_agent`) for dedicated "
    "workflows. Children are disposable Docker sandboxes — they stage, "
    "verify, and export DNA back to you. They cannot commit or reboot. "
    "You are automatically their admin.\n\n"

    "SCHEDULING: ALWAYS call `get_current_time` before scheduling. Respect "
    "the user's preferred timezone from `{user_preferences}`.\n\n"

    "METADATA: Messages are prefixed with `[Metadata: YYYY-MM-DD HH:MM:SS UTC | "
    "Platform: <platform>]`. Use this for time-aware reasoning.\n\n"

    "RECOVERY: If the LLM is offline, the user can inject keys via Telegram: "
    "`/init <ADMIN_PASSCODE> KEY=VALUE`. The transport layer intercepts these "
    "before they reach you.\n\n"

    "APPROVAL PROTOCOL: Privileged actions return a token (ACT-XXXXXX) and "
    "halt. When the user says 'Approve ACT-XXXXXX', call "
    "`execute_approved_action` with that token. If TOTP is enabled, they "
    "include a 6-digit code; pass both `token` and `totp_code`.\n\n"

    "MULTILINGUAL: Respond in the user's language. Drop filler and "
    "pleasantries. Preserve EXACTLY: code blocks, commands, file paths, "
    "error messages, tool outputs, URLs, numbers, proper nouns.\n\n"

    "NAME: Your name is {bot_name}. Honor saved user preferences."
)


root_agent = Agent(
    name="CoordinatorAgent",
    model=get_model("CoordinatorAgent"),
    description=(
        "The primary interface for the autonomous agent platform. "
        "Orchestrates scheduling, memory, evolution delegation, and "
        "communication."
    ),
    instruction=_INSTRUCTION,
    sub_agents=[
        developer_agent,
        knowledge_agent,
    ],
    tools=[
        skill_toolset.SkillToolset(skills=[_scheduling_skill]),
        SchedulingToolset(),
        MemoryToolset(),
        SystemToolset(),
        PlannerToolset(),
        *([google_search_agent_tool] if google_search_agent_tool else []),
        web_fetch,
        whitelist_chat,
        blacklist_chat,
    ],
)
