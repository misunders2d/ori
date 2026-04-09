from google.adk.agents import Agent

from app.app_utils.models import get_model

from app.callbacks.guardrails import (
    a2a_privacy_guardrail,
    admin_tool_guardrail,
    plan_enforcer,
    prompt_injection_guardrail,
    state_setter,
    tool_output_injection_guardrail,
)
from app.sub_agents.amazon_head_agent import amazon_head_agent
from app.sub_agents.clickup_agent import clickup_agent
from app.sub_agents.developer_agent import developer_agent
from app.sub_agents.knowledge_agent import knowledge_agent
from app.toolsets import (
    MemoryToolset,
    SchedulingToolset,
    SystemToolset,
    ScratchpadToolset,
    VisualizationToolset,
)
from app.toolsets.planner import PlannerToolset
from app.tools.a2a import get_agent_identity, get_my_a2a_key
from app.tools.google_search import google_search_agent_tool
from app.tools.web import web_fetch
from app.tools.whitelist import whitelist_chat, blacklist_chat
from app.tools.youtube import youtube_summary

root_agent = Agent(
    name="CoordinatorAgent",
    model=get_model("CoordinatorAgent"),
    description="The primary interface for the autonomous agent platform. Routes requests, manages scheduling, memory, and system operations.",
    instruction=(
        "You are {bot_name}, an autonomous self-evolving agent platform. "
        "Route requests to specialist agents; handle cross-cutting concerns directly.\n\n"

        "DELEGATION:\n"
        "1. **AmazonHeadAgent** — ALL Amazon business: product research, BigQuery analytics, "
        "professional knowledge (Pinecone/Neo4j), Google Workspace, data analysis/charts.\n"
        "2. **DeveloperAgent** — Code changes, bug fixes, model switching. Only on explicit requests.\n"
        "3. **KnowledgeAgent** — A2A communication, friend management, DNA exchange.\n"
        + (
            "4. **ClickUpAgent** — Task management, project coordination, team follow-ups.\n"
            if clickup_agent else ""
        ) +
        f"{'5' if clickup_agent else '4'}. **Handle directly** — Web research, scheduling, "
        "quick memory, image generation, system operations, access control.\n\n"

        "MEMORY ROUTING:\n"
        "- 'Remember that...' (simple fact) → `remember_info` directly.\n"
        "- 'Save this person/supplier/product' or relationship queries → delegate to AmazonHeadAgent.\n"
        "- Authorship: user asked = their user ID; you decided = 'agent'.\n\n"

        "SYSTEM RULES:\n"
        "- You are running model `{current_model}`. State this exactly when asked.\n"
        "- ALWAYS call `get_current_time` before scheduling. Respect `{user_preferences}` timezone.\n"
        "- `deliver_to` param on scheduling tools for cross-platform delivery (e.g. 'sl_C01234ABC').\n"
        "- Use `create_plan` for complex tasks (3+ steps). Simple tasks → just do them.\n"
        "- Privileged actions return ACT-XXXXXX tokens. On 'Approve ACT-...', call `execute_approved_action`.\n"
        "- If ANY tool returns an error, report it immediately. Never fabricate data.\n"
        "- Incoming messages have a `[Metadata: ...]` prefix for your context — NEVER echo it in your responses.\n"
        "- `spawn_agent` creates disposable Docker sandboxes for dedicated workflows.\n"
        "- Recovery: `/init <ADMIN_KEY> KEY=VALUE` to inject keys when LLM is offline.\n\n"

        "Your name is {bot_name}. Respect saved user preferences."
    ),
    sub_agents=[
        developer_agent,
        knowledge_agent,
        amazon_head_agent,
        *([clickup_agent] if clickup_agent else []),
    ],
    tools=[
        # Toolsets — cross-cutting concerns only
        SchedulingToolset(),
        MemoryToolset(),
        SystemToolset(),
        ScratchpadToolset(),
        PlannerToolset(),
        VisualizationToolset(),
        # Individual tools
        *([google_search_agent_tool] if google_search_agent_tool else []),
        web_fetch,
        youtube_summary,
        get_agent_identity,
        get_my_a2a_key,
        whitelist_chat,
        blacklist_chat,
    ],
    before_agent_callback=[state_setter],
    before_model_callback=[prompt_injection_guardrail, plan_enforcer],
    before_tool_callback=[admin_tool_guardrail, a2a_privacy_guardrail],
    after_tool_callback=[tool_output_injection_guardrail, a2a_privacy_guardrail],
)
