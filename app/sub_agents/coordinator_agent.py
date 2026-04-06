from google.adk.agents import Agent

from app.app_utils.models import get_model

from app.callbacks.guardrails import (
    a2a_privacy_guardrail,
    admin_tool_guardrail,
    prompt_injection_guardrail,
    state_setter,
    tool_output_injection_guardrail,
)
from app.sub_agents.developer_agent import developer_agent
from app.sub_agents.knowledge_agent import knowledge_agent
from app.toolsets import (
    MemoryToolset,
    SchedulingToolset,
    SystemToolset,
)
from app.tools.a2a import get_agent_identity
from app.tools.google_search import google_search_agent_tool
from app.tools.web import web_fetch
from app.tools.whitelist import whitelist_chat, blacklist_chat
from app.tools.youtube import youtube_summary

root_agent = Agent(
    name="CoordinatorAgent",
    model=get_model("CoordinatorAgent"),
    description="The primary interface for the autonomous agent platform. Orchestrates scheduling, memory, evolution, and communication.",
    instruction=(
        "You are {bot_name}, an autonomous self-evolving agent platform. "
        "Your job is to orchestrate tasks, remember context, and delegate specialized work.\n\n"

        "DELEGATION:\n"
        "1. For self-evolution (code changes, bug fixes, adding features): Delegate to DeveloperAgent.\n"
        "2. For A2A communication, friend management, DNA exchange: Delegate to KnowledgeAgent.\n"
        "3. For everything else (research, scheduling, memory, access control): Handle directly.\n\n"

        "SPAWNING: You can spawn child agents (`spawn_agent`) for dedicated workflows. "
        "Children are disposable Docker sandboxes — they stage, verify, and export DNA back to you. "
        "They cannot commit or reboot. You are automatically their admin.\n\n"

        "SCHEDULING: ALWAYS call `get_current_time` before scheduling. "
        "Respect the user's preferred timezone from `{user_preferences}`.\n\n"

        "METADATA: Messages are prefixed with `[Metadata: YYYY-MM-DD HH:MM:SS UTC | Platform: platform]`.\n\n"

        "RECOVERY: If the LLM is offline, the user can inject keys via Telegram:\n"
        "`/init <ADMIN_KEY> KEY=VALUE`\n\n"

        "APPROVAL PROTOCOL: Privileged actions return a token (ACT-XXXXXX). "
        "When the user says 'Approve ACT-XXXXXX', call `execute_approved_action` with that token. "
        "If they provide a 6-digit code, pass both the token and `totp_code`.\n\n"

        "EAGER DELEGATION: Answer questions directly first. "
        "Delegate to DeveloperAgent ONLY on explicit action requests ('fix it', 'write the code').\n\n"

        "NAME: Your name is {bot_name}. Respect saved user preferences."
    ),
    sub_agents=[
        developer_agent,
        knowledge_agent,
    ],
    tools=[
        # Toolsets
        SchedulingToolset(),
        MemoryToolset(),
        SystemToolset(),
        # Individual tools
        *([google_search_agent_tool] if google_search_agent_tool else []),
        web_fetch,
        youtube_summary,
        get_agent_identity,
        whitelist_chat,
        blacklist_chat,
    ],
    before_agent_callback=[state_setter],
    before_model_callback=prompt_injection_guardrail,
    before_tool_callback=[admin_tool_guardrail, a2a_privacy_guardrail],
    after_tool_callback=[tool_output_injection_guardrail, a2a_privacy_guardrail],
)