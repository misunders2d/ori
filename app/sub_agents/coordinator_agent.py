import os
import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

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
    CreativesToolset,
)
from app.toolsets.planner import PlannerToolset
from app.tools.a2a import get_agent_identity, get_my_a2a_key
from app.tools.google_search import google_search_agent_tool
from app.tools.web import web_fetch
from app.tools.whitelist import whitelist_chat, blacklist_chat
from app.tools.youtube import youtube_summary
from app.tools.slack import (
    slack_post_message,
    slack_list_channels,
    slack_read_history,
)

_slack_enabled = bool(os.environ.get("SLACK_BOT_TOKEN", "").strip())
_slack_tools = (
    [slack_post_message, slack_list_channels, slack_read_history] if _slack_enabled else []
)

_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_scheduling_skill = load_skill_from_dir(_skills_dir / "scheduling-skill")

root_agent = Agent(
    name="CoordinatorAgent",
    model=get_model("CoordinatorAgent"),
    description="The primary interface for the autonomous agent platform. Routes requests, manages scheduling, memory, and system operations.",
    instruction=(
        "You are {bot_name}, an autonomous self-evolving agent platform. "
        "Route requests to specialist agents; handle cross-cutting concerns directly.\n\n"

        "DELEGATION:\n"
        "1. **AmazonHeadAgent** — ALL Amazon business: product research, BigQuery analytics, "
        "knowledge graph (Neo4j) for memories + people, Google Workspace, data analysis/charts.\n"
        "2. **DeveloperAgent** — Code changes, bug fixes, model switching. Only on explicit requests.\n"
        "3. **KnowledgeAgent** — A2A communication, friend management, DNA exchange.\n"
        + (
            "4. **ClickUpAgent** — Task management, project coordination, team follow-ups.\n"
            if clickup_agent else ""
        ) +
        f"{'5' if clickup_agent else '4'}. **Handle directly** — Web research, scheduling, "
        "quick memory, AI image generation (`generate_image`, `enhance_image_prompt`), "
        "system operations, access control.\n\n"

        "MEMORY ROUTING (be strict — default to Neo4j):\n"
        "- Use `remember_info` (local LanceDB) ONLY for the user's interaction preferences "
        "about you — tone, language, formality, how they want to be addressed, short-lived "
        "conversational context. Example: 'call me Sergey, not Mr. Demchenko', "
        "'respond in Russian when I write in Russian'.\n"
        "- EVERYTHING else the user asks you to remember goes to Neo4j via AmazonHeadAgent. "
        "Facts, decisions, procedures, naming conventions, policies, incidents, deploy notes, "
        "people, companies, products, suppliers, links, playbooks — anything that might be "
        "useful to another agent or to future-you. Delegate to AmazonHeadAgent.\n"
        "- When in doubt, delegate to AmazonHeadAgent. Neo4j is the durable shared store; "
        "LanceDB is a per-user interaction-style scratchpad. Misrouting a business fact to "
        "LanceDB silently loses it for everyone else.\n"
        "- Authorship is captured automatically via a graph edge from the caller's :Person "
        "node; do not pass or fabricate author values.\n\n"

        "SCHEDULING / REMINDER ROUTING (STRICT):\n"
        "- ANY 'remind me / every X / at <time> / in N minutes' request → handle DIRECTLY with the "
        "scheduling tools. Load `scheduling-skill` for cron format, `deliver_to` channel routing "
        "(`sl_<id>` Slack, `tg_<id>` Telegram), and plan-enforcement-in-task patterns.\n"
        "- NEVER delegate scheduling to ClickUpAgent or AmazonWorkspaceAgent.\n"
        "- ClickUp only on explicit 'create a ClickUp task / assign in ClickUp' wording.\n"
        "- Google Calendar (via AmazonHeadAgent → AmazonWorkspaceAgent) only on explicit "
        "'add to calendar / create event / invite <attendees>'.\n"
        "- If ambiguous, default to scheduling and ask if a calendar event / ClickUp task is also wanted.\n\n"

        "SYSTEM RULES:\n"
        "- You are running model `{current_model}`. State this exactly when asked.\n"
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
        skill_toolset.SkillToolset(skills=[_scheduling_skill]),
        # Toolsets — cross-cutting concerns only
        SchedulingToolset(),
        MemoryToolset(),
        SystemToolset(),
        ScratchpadToolset(),
        PlannerToolset(),
        CreativesToolset(),
        # Individual tools
        *([google_search_agent_tool] if google_search_agent_tool else []),
        web_fetch,
        youtube_summary,
        get_agent_identity,
        get_my_a2a_key,
        whitelist_chat,
        blacklist_chat,
        *_slack_tools,
    ],
    before_agent_callback=[state_setter],
    before_model_callback=[prompt_injection_guardrail, plan_enforcer],
    before_tool_callback=[admin_tool_guardrail, a2a_privacy_guardrail],
    after_tool_callback=[tool_output_injection_guardrail, a2a_privacy_guardrail],
)
