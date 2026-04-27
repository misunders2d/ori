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

import os
import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.agents.amazon_head_agent import amazon_head_agent
from app.agents.clickup_agent import clickup_agent
from app.agents.developer import developer_agent
from app.agents.knowledge import knowledge_agent
from app.tools.a2a import get_agent_identity, get_my_a2a_key, list_friends
from app.tools.google_search import google_search_agent_tool
from app.tools.model_tools import (
    get_llm_provider,
    list_available_models,
    switch_llm_provider,
)
from app.tools.web import web_fetch
from app.tools.whitelist import blacklist_chat, whitelist_chat
from app.toolsets import (
    CreativesToolset,
    IntegrationToolset,
    MemoryToolset,
    PlannerToolset,
    SchedulingToolset,
    ScratchpadToolset,
    SystemToolset,
)
from app.util.models import get_model

_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_scheduling_skill = load_skill_from_dir(_skills_dir / "scheduling-skill")
_configuration_skill = load_skill_from_dir(_skills_dir / "configuration-skill")
_approval_skill = load_skill_from_dir(_skills_dir / "approval-skill")
_scratchpad_skill = load_skill_from_dir(_skills_dir / "scratchpad-skill")
_spawn_skill = load_skill_from_dir(_skills_dir / "spawn-skill")
_model_swap_skill = load_skill_from_dir(_skills_dir / "model-swap-skill")


# Constitutional only. Procedural detail lives in skills, loaded on demand.
#
# `{bot_name}` is intentionally NOT a session-state template variable.
# ADK 2.0's `inject_session_state` reads from
# `invocation_context.session.state[var_name]` at instruction-build time,
# and on the workflow-node path that state object isn't always the one
# our plugins mutate. Result was a hard KeyError on every inbound A2A
# request. bot_name is env-driven anyway, so we resolve it at module
# load time — no template variable, no failure mode.
_BOT_NAME = os.environ.get("BOT_NAME", "Ori")


_INSTRUCTION = (
    f"You are {_BOT_NAME}, an autonomous self-evolving agent platform. "
    "Your job is to orchestrate tasks, remember context, and delegate "
    "specialized work.\n\n"

    "DELEGATION:\n"
    "- Self-evolution (code changes, bug fixes, adding features) → "
    "DeveloperAgent.\n"
    "- Model CHANGES (`set_agent_model`, `verify_model_reachable`) → "
    "DeveloperAgent. The probe-before-persist lives there and is admin-gated.\n"
    "- A2A communication, friend management, DNA exchange → KnowledgeAgent.\n"
    "- Amazon business operations (ASINs, SKUs, Keepa, SP-API, Helium10, "
    "BigQuery analytics, Google Workspace for the business, knowledge graph, "
    "data analysis, charts, file exports) → AmazonHeadAgent.\n"
    "- ClickUp tasks (when ClickUpAgent is present) → ClickUpAgent.\n"
    "- Everything else (research, scheduling, memory, perimeter, plans, "
    "API keys, spawning children, approvals) — handle DIRECTLY.\n\n"

    "EAGER DELEGATION RULE: answer knowledge questions directly first. "
    "Delegate to DeveloperAgent ONLY on explicit action requests ('fix it', "
    "'write the code', 'integrate X'). Delegate to KnowledgeAgent ONLY on "
    "outbound action requests (call X, send Y to friend, exchange DNA).\n\n"

    "INSPECTION QUESTIONS — call the tool, never guess from context:\n"
    "- 'Which model are you on?' / 'list models' → `list_available_models`.\n"
    "- 'What's your A2A URL?' / 'agent card' / 'tunnel url' → "
    "`get_agent_identity` (returns the live agent card with URL).\n"
    "- 'What's your A2A API key?' (admin only — never leak it to "
    "non-admins) → `get_my_a2a_key`.\n"
    "- 'Who are your friends?' / 'list friends' → `list_friends`.\n"
    "If the tool returns an error or empty value, RELAY IT VERBATIM. "
    "Hallucinating a value (or saying 'not available' when you didn't "
    "even call the tool) is forbidden.\n\n"

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
    "Platform: <platform>]`. Use this for time-aware reasoning. To honor "
    "the user's timezone, call `get_user_preferences` (or "
    "`recall_human_preferences`) before scheduling.\n\n"

    "RECOVERY: If the LLM is offline, the user can inject keys via Telegram: "
    "`/init <ADMIN_PASSCODE> KEY=VALUE`. The transport layer intercepts these "
    "before they reach you.\n\n"

    "PLAN MECHANICS (when an active plan is present — you'll see a "
    "'PLAN STATUS' directive in your system instruction):\n"
    "- Planner tools (`get_next_step`, `complete_step`, `create_plan`, "
    "`abandon_plan`) belong to YOU. Sub-agents do NOT have these and MUST "
    "NEVER be asked to call them.\n"
    "- Pattern: call `get_next_step` → delegate the step's WORK content to "
    "the right sub-agent (e.g. AmazonHeadAgent for ASIN/BigQuery/Workspace) "
    "→ when the sub-agent returns, YOU call `complete_step` with the "
    "result summary. Repeat until `complete_step` reports 'All steps "
    "completed!'.\n"
    "- Do NOT include 'call complete_step when you're done' in your "
    "delegation prompt to the sub-agent. The sub-agent doesn't have it; "
    "instructing it to call a missing tool would loop the run.\n\n"

    f"NAME: Your name is {_BOT_NAME}. Honor saved user preferences."
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
        amazon_head_agent,
        *([clickup_agent] if clickup_agent else []),
    ],
    tools=[
        skill_toolset.SkillToolset(skills=[
            _scheduling_skill,
            _configuration_skill,
            _approval_skill,
            _spawn_skill,
            _model_swap_skill,
            _scratchpad_skill,
        ]),
        SchedulingToolset(),
        MemoryToolset(),
        SystemToolset(),
        PlannerToolset(),
        IntegrationToolset(),
        ScratchpadToolset(),
        CreativesToolset(),
        *([google_search_agent_tool] if google_search_agent_tool else []),
        web_fetch,
        whitelist_chat,
        blacklist_chat,
        # Read-only inspection tools. Answers questions like "which model
        # are you on?" / "what's your A2A URL?" / "who are your friends?"
        # without delegation. Mutating counterparts (set_agent_model,
        # add_friend, call_friend, etc.) stay on Developer / Knowledge.
        list_available_models,
        get_llm_provider,
        switch_llm_provider,
        get_agent_identity,
        get_my_a2a_key,
        list_friends,
    ],
)
