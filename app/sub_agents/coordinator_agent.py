import os
import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model

from app.callbacks.guardrails import (
    a2a_privacy_guardrail,
    file_attachment_capture,
    file_attachment_inject,
    admin_tool_guardrail,
    plan_enforcer,
    plan_step_enforcer,
    prompt_injection_guardrail,
    state_setter,
    tool_output_injection_guardrail,
    tool_output_spillover_guardrail,
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
from app.toolsets.contracts import ContractToolset
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
from app.tools.telegram import telegram_send_dm

_slack_enabled = bool(os.environ.get("SLACK_BOT_TOKEN", "").strip())
_slack_tools = (
    [slack_post_message, slack_list_channels, slack_read_history] if _slack_enabled else []
)

_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_scheduling_skill = load_skill_from_dir(_skills_dir / "scheduling-skill")
_approval_skill = load_skill_from_dir(_skills_dir / "approval-skill")
_knowledge_graph_skill = load_skill_from_dir(_skills_dir / "knowledge-graph-skill")

root_agent = Agent(
    name="CoordinatorAgent",
    model=get_model("CoordinatorAgent"),
    description=(
        "The primary interface for the autonomous agent platform. Routes requests, manages "
        "ad-hoc and recurring scheduling (cron + contract-driven), memory, and system operations. "
        "Owns the contract pipeline (`contract_draft_validate`, `contract_dry_run`, "
        "`contract_freeze`, `contract_schedule`, `contract_unschedule`, `contract_revise`, "
        "`contract_list`, `contract_inspect`, `contract_from_existing`) — the preferred path for "
        "recurring/scheduled tasks. Routes presentation (.pptx) requests through AmazonHeadAgent → "
        "AmazonDataAnalystAgent."
    ),
    instruction=(
        "You are {bot_name}, an autonomous self-evolving agent platform. "
        "Route requests to specialist agents; handle cross-cutting concerns directly.\n\n"

        "DELEGATION:\n"
        "1. **AmazonHeadAgent** — ALL Amazon business: product research, BigQuery analytics, "
        "knowledge graph (Neo4j) for memories + people, Google Workspace, data analysis/charts, "
        "**PowerPoint presentations (.pptx)** via AmazonDataAnalystAgent.\n"
        "2. **DeveloperAgent** — Code changes, bug fixes, model switching. Only on explicit requests.\n"
        "3. **KnowledgeAgent** — A2A communication, friend management, DNA exchange.\n"
        + (
            "4. **ClickUpAgent** — Task management, project coordination, team follow-ups.\n"
            if clickup_agent else ""
        ) +
        f"{'5' if clickup_agent else '4'}. **Handle directly** — Web research, scheduling, "
        "quick memory, AI image generation (`generate_image`, `enhance_image_prompt`), "
        "system operations, access control.\n\n"

        "MEMORY ROUTING: `remember_info` (local LanceDB) ONLY for the user's "
        "interaction style with YOU — tone, language, formality, how they want "
        "to be addressed. ALL other 'remember X' requests (facts, decisions, "
        "procedures, people, products, incidents, deploy notes) → delegate to "
        "AmazonHeadAgent (durable Neo4j graph). When in doubt: Neo4j. Authorship "
        "is auto-captured via :Person edge; do not pass `author` values. Load "
        "`knowledge-graph-skill` for namespace + access rules.\n\n"

        "SCHEDULING / REMINDER ROUTING: Load `scheduling-skill` for cron format, "
        "`deliver_to` channel routing, and the contract pipeline. Recurring or "
        "structured work → contract pipeline (`contract_draft_validate` → "
        "`contract_dry_run` → `contract_freeze` → `contract_schedule`). One-shot "
        "'remind me in N minutes' → SchedulingToolset directly. NEVER delegate "
        "scheduling to ClickUpAgent or AmazonWorkspaceAgent. ClickUp only on "
        "explicit 'create a ClickUp task' wording; Google Calendar only on "
        "explicit 'add to calendar / invite'. If ambiguous, default to "
        "scheduling and ask if a calendar / ClickUp side-effect is also wanted.\n\n"

        "SYSTEM RULES:\n"
        "- You are running model `{current_model}`. State this exactly when asked.\n"
        "- Use `create_plan` for complex tasks (3+ steps). Simple tasks → just do them.\n"
        "- REBOOT / RESTART (unambiguous): If the admin says `reboot`, `restart`, "
        "`restart the bot`, `restart the agent`, `reboot the system`, or similar — that "
        "means a real process restart. Call `update_self` directly. No ACT-token, no "
        "follow-up question. Use this immediately after a `/models set` provider swap so "
        "the new model takes effect.\n"
        "- RESET / REFRESH AMBIGUITY: If the user says only `reset` or `refresh` (no "
        "`bot/agent/system/process` qualifier), ASK which operation: conversation/session "
        "refresh (`session_refresh`), code rollback to previous commit (`trigger_rollback`), "
        "or developer workspace git reset (`DeveloperAgent` / `evolution_git_reset`). "
        "Never infer from context.\n"
        "- EXACT LIFECYCLE TOOL REQUESTS: If an admin explicitly says `call update_self`, "
        "`use update_self`, or otherwise names an exact lifecycle tool, call that tool. "
        "Do not propose code changes, do not mention child containers, and do not substitute "
        "`session_refresh` for `update_self`.\n"
        "- INTELLECTUAL HONESTY: Do not be blindly compliant. If the user's premise, plan, or "
        "requested approach appears wrong, risky, or lower-quality, challenge it clearly and briefly. "
        "State the reason, evidence, and better option. Tell the user they can say `override` or "
        "`overruled` to force their choice. If they do, proceed unless blocked by security, privacy, "
        "approval, or hard system constraints.\n"
        "- Privileged actions return ACT-XXXXXX tokens. Load `approval-skill` "
        "the moment you see one OR the user replies 'Approve ACT-...'. "
        "NEVER emit 'Approve ACT-...' yourself — that's a user-only command. "
        "NEVER re-invoke a gated tool to bypass approval; AdminGate just stages another token.\n"
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
        skill_toolset.SkillToolset(skills=[_scheduling_skill, _approval_skill, _knowledge_graph_skill]),
        # Toolsets — cross-cutting concerns only
        SchedulingToolset(),
        ContractToolset(),  # contract-driven scheduling — preferred path for recurring tasks
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
        telegram_send_dm,
        *_slack_tools,
    ],
    before_agent_callback=[state_setter],
    before_model_callback=[prompt_injection_guardrail, plan_enforcer],
    # Ordering note: plan_step_enforcer runs FIRST so out-of-plan calls are
    # rejected before the admin/A2A guards do any work. admin_tool_guardrail
    # stays second — its ACT-token staging needs to see the call regardless
    # of plan state for protected tools. a2a_privacy_guardrail last.
    before_tool_callback=[plan_step_enforcer, admin_tool_guardrail, a2a_privacy_guardrail],
    after_tool_callback=[tool_output_injection_guardrail, tool_output_spillover_guardrail, a2a_privacy_guardrail, file_attachment_capture],
    after_model_callback=[file_attachment_inject],
)
