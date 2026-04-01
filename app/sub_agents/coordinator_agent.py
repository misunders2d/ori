import google.adk.tools
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.planners import BuiltInPlanner
from google.genai import types

from app.callbacks.guardrails import (
    a2a_privacy_guardrail,
    admin_tool_guardrail,
    prompt_injection_guardrail,
    state_setter,
    tool_output_injection_guardrail,
)
from app.sub_agents.developer_agent import developer_agent
from app.sub_agents.knowledge_agent import knowledge_agent
from app.tools import (
    configure_integration,
    delete_scheduled_task,
    edit_scheduled_task,
    get_current_time,
    get_user_preferences,
    list_integrations,
    list_scheduled_tasks,
    remove_integration,
    run_system_task_now,
    save_user_preferences,
    schedule_one_off_task,
    schedule_recurring_system_task,
    schedule_recurring_task,
    schedule_system_task,
    session_refresh,
    set_planner_mode,
    trigger_rollback,
    update_self,
    web_fetch,
)
from app.tools.a2a import get_agent_identity
from app.tools.auth import (
    check_connection,
    complete_auth_code,
    connect_to_platform,
    disconnect_platform,
    list_platforms,
    register_platform,
    remove_platform_registration,
)
from app.tools.channel_summarizer import summarize_channel
from app.tools.google_search import google_search_agent_tool
from app.tools.health import report_health
from app.tools.memory import (
    recall_human_preferences,
    recall_technical_context,
    remember_info,
    search_memory,
    modify_memory,
    delete_memory,
)
from app.tools.origins import analyze_upstream_file, check_upstream
from app.tools.system import check_active_tasks, execute_approved_action
from app.tools.whitelist import blacklist_chat, list_access_control, unwhitelist_chat, whitelist_chat

root_agent = Agent(
    name="CoordinatorAgent",
    model=Gemini(model="gemini-3.1-pro-preview"),
    description="The primary interface for the autonomous daemon. Receives intent and commands, and delegates to specialized sub-agents.",
    instruction=(
        "You are {bot_name}, an autonomous self-evolving agent. "
        "Your job is to orchestrate management, scheduling, and development.\n\n"
        "1. For general research or complex web tasks: Use the google search and web fetch tools directly. "
        "2. For scheduling/reminders: ALWAYS call `get_current_time` first to know current time. "
        "3. For self-evolution (code changes, improvements, fixing bugs): Delegate to DeveloperAgent. "
        "4. For A2A collaboration and knowledge management (Ori-Net): Delegate to KnowledgeAgent. "
        "5. For session management: Use `session_refresh`. "
        "6. For OAuth2 platform connections: Use `list_platforms`, `register_platform`, `connect_to_platform`, etc. "
        "7. For Access Control: Use `whitelist_chat`, `blacklist_chat`, `unwhitelist_chat`, and `list_access_control`.\n\n"
        "TELEGRAM RECOVERY MECHANISM: If the LLM is offline or the Google API key is expired, the user can inject keys "
        "directly via the Telegram chat bar using this EXACT syntax:\n"
        "`/init <ADMIN_KEY> KEY=VALUE` (e.g., `/init my-secret-pass GOOGLE_API_KEY=AIzaSy...`)\n"
        "NEVER provide the wrong format to the user.\n\n"
        "TOKEN APPROVAL PROTOCOL: Highly privileged system actions (updates, restarts, integration changes) "
        "are protected by a staging mechanism. When you attempt such an action, the system will return a token (e.g., ACT-XXXXXX). "
        "The user must then provide this token to you. When the user says 'Approve ACT-XXXXXX' or similar, "
        "you MUST call the `execute_approved_action` tool with that token. "
        "If the user also provides a 6-digit code, pass BOTH the token and the `totp_code` to the tool.\n\n"
        "SCHEDULING MANDATE: Only YOU (CoordinatorAgent) can launch background/system tasks via `run_system_task_now`, etc. "
        "Background tasks automatically deliver their reports to the user when finished. "
        "If a user asks 'is it done?', use `check_active_tasks` to see the real-time status.\n\n"
        "EAGER DELEGATION MANDATE: If the user reports a bug, shares a screenshot, or asks a question about the system, you must answer directly first. "
        "Transfer to DeveloperAgent ONLY if there is an explicit call to action (e.g., 'fix it', 'write the code').\n\n"
        "NAME: Your name is {bot_name}. Always refer to yourself by this name. Respect saved user preferences."
    ),
    sub_agents=[
        developer_agent,
        knowledge_agent,
    ],
    tools=[
        get_current_time,
        schedule_one_off_task,
        schedule_recurring_task,
        list_scheduled_tasks,
        edit_scheduled_task,
        delete_scheduled_task,
        configure_integration,
        remove_integration,
        list_integrations,
        list_platforms,
        register_platform,
        connect_to_platform,
        complete_auth_code,
        check_connection,
        disconnect_platform,
        remove_platform_registration,
        report_health,
        check_upstream,
        analyze_upstream_file,
        remember_info,
        search_memory,
        modify_memory,
        delete_memory,
        recall_human_preferences,
        recall_technical_context,
        get_agent_identity,
        run_system_task_now,
        schedule_system_task,
        schedule_recurring_system_task,
        update_self,
        session_refresh,
        trigger_rollback,
        set_planner_mode,
        save_user_preferences,
        get_user_preferences,
        google_search_agent_tool,
        web_fetch,
        check_active_tasks,
        execute_approved_action,
        whitelist_chat,
        blacklist_chat,
        unwhitelist_chat,
        list_access_control,
        summarize_channel,
    ],
    before_agent_callback=[state_setter],
    before_model_callback=prompt_injection_guardrail,
    before_tool_callback=[admin_tool_guardrail, a2a_privacy_guardrail],
    after_tool_callback=[tool_output_injection_guardrail, a2a_privacy_guardrail],
)
