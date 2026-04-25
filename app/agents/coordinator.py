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
    IntegrationToolset,
    MemoryToolset,
    PlannerToolset,
    SchedulingToolset,
    SystemToolset,
)
from app.tools.google_search import google_search_agent_tool
from app.tools.model_tools import list_available_models
from app.tools.web import web_fetch
from app.tools.whitelist import blacklist_chat, whitelist_chat
from app.util.models import get_model


_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_scheduling_skill = load_skill_from_dir(_skills_dir / "scheduling-skill")
_configuration_skill = load_skill_from_dir(_skills_dir / "configuration-skill")
_approval_skill = load_skill_from_dir(_skills_dir / "approval-skill")
_spawn_skill = load_skill_from_dir(_skills_dir / "spawn-skill")
_model_swap_skill = load_skill_from_dir(_skills_dir / "model-swap-skill")


# Constitutional only. Procedural detail lives in skills, loaded on demand.
_INSTRUCTION = (
    "You are {bot_name}, an autonomous self-evolving agent platform. "
    "Your job is to orchestrate tasks, remember context, and delegate "
    "specialized work.\n\n"

    "DELEGATION:\n"
    "- Self-evolution (code changes, bug fixes, adding features) → "
    "DeveloperAgent.\n"
    "- Model CHANGES (`set_agent_model`, `verify_model_reachable`) → "
    "DeveloperAgent. The probe-before-persist lives there and is admin-gated.\n"
    "- A2A communication, friend management, DNA exchange → KnowledgeAgent.\n"
    "- Everything else (research, scheduling, memory, perimeter, plans, "
    "API keys, spawning children, approvals) — handle DIRECTLY.\n\n"

    "ASKED 'WHICH MODEL ARE YOU ON?' or any model-INSPECTION question: "
    "call `list_available_models` directly. Don't guess from memory and "
    "don't delegate — you have the read-only tool yourself. Only swap "
    "operations require DeveloperAgent.\n\n"

    "EAGER DELEGATION RULE: answer knowledge questions directly first. "
    "Delegate to DeveloperAgent ONLY on explicit action requests ('fix it', "
    "'write the code', 'integrate X'). Knowledge questions about A2A or "
    "friends do NOT require KnowledgeAgent — answer from your own context.\n\n"

    "SKILLS — load these on demand:\n"
    "- `scheduling-skill` for any reminder, schedule, automation, cron task.\n"
    "- `configuration-skill` for API keys, OAuth, integrations.\n"
    "- `approval-skill` when a tool returns ACT-XXXXXX or the user replies "
    "'Approve ACT-...'.\n"
    "- `spawn-skill` for spawning disposable child agents.\n"
    "- `model-swap-skill` BEFORE building any model string for "
    "set_agent_model / verify_model_reachable. Critical for OpenRouter — "
    "the prefix must be `openrouter/<vendor>/<model>`, not the bare "
    "vendor name.\n\n"

    "METADATA: Messages are prefixed with `[Metadata: YYYY-MM-DD HH:MM:SS UTC | "
    "Platform: <platform>]`. Use this for time-aware reasoning. Honor the "
    "user's timezone from `{user_preferences}`.\n\n"

    "RECOVERY: If the LLM is offline, the user can inject keys via Telegram: "
    "`/init <ADMIN_PASSCODE> KEY=VALUE`. The transport layer intercepts these "
    "before they reach you.\n\n"

    "NAME: Your name is {bot_name}. Honor saved user preferences."
)


root_agent = Agent(
    name="CoordinatorAgent",
    # mode='chat' is required when this Agent runs as a Workflow node.
    # Workflow node default is 'single_turn' which forces
    # include_contents='none' — strips all conversation history. With
    # 'chat', the LlmAgent keeps its default include_contents='default'
    # and sees the full session events as conversation history.
    # See google.adk.workflow._llm_agent_wrapper:run_llm_agent_as_node.
    mode="chat",
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
        skill_toolset.SkillToolset(skills=[
            _scheduling_skill,
            _configuration_skill,
            _approval_skill,
            _spawn_skill,
            _model_swap_skill,
        ]),
        SchedulingToolset(),
        MemoryToolset(),
        SystemToolset(),
        PlannerToolset(),
        IntegrationToolset(),
        *([google_search_agent_tool] if google_search_agent_tool else []),
        web_fetch,
        whitelist_chat,
        blacklist_chat,
        # Read-only — answers "which model are you on?" without delegation.
        # Mutating tools (set_agent_model, verify_model_reachable) stay on
        # DeveloperAgent because they hit the LLM (probe) and should be
        # admin-gated.
        list_available_models,
    ],
)
