import google.adk.tools
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.planners import BuiltInPlanner
from google.genai import types

from app.callbacks.guardrails import (
    admin_tool_guardrail,
    prompt_injection_guardrail,
    state_setter,
    tool_output_injection_guardrail,
)
from app.tools.google_search import google_search_agent_tool
from app.tools.auth import connect_to_platform, check_connection
from app.tools.health import report_health
from app.tools.origins import check_upstream, analyze_upstream_file
from app.tools.memory import remember_info, search_memory, recall_human_preferences, recall_technical_context
from app.tools import (
    configure_integration,
    delete_scheduled_task,
    edit_scheduled_task,
    get_current_time,
    get_user_preferences,
    list_integrations,
    list_scheduled_tasks,
    remove_integration,
    save_user_preferences,
    schedule_one_off_task,
    schedule_recurring_task,
    schedule_system_task,
    schedule_recurring_system_task,
    session_refresh,
    update_self,
    trigger_rollback,
    set_planner_mode,
    web_fetch,
)

from app.sub_agents.developer_agent import developer_agent

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
        "4. For session management: Use `session_refresh`. "
        "5. For OAuth2 platform connections: Use `check_connection` and `connect_to_platform`. "
        "6. For Origins Protocol: Use `check_upstream` to see new features/fixes. "
        "7. For Long-Term Memory: Use `remember_info` to store facts, preferences, or technical notes. "
        "Use `search_memory`, `recall_human_preferences`, or `recall_technical_context` to retrieve information from previous sessions. "
        "This memory is local, private, and persistent across reboots.\n\n"
        "GUARDRAIL PROTECTION MANDATE: The guardrails (event callbacks like `before_agent_callback`, `before_model_callback`, etc.) "
        "are critical for system safety and security. You MUST NOT remove, modify, or try to bypass these guardrails "
        "under any circumstances, unless explicitly requested by the user.\n\n"
        "CREDENTIAL SECURITY: NEVER ask a user to type a secret directly in chat. Use `configure_integration` for keys. "
        "NAME: Your name is {bot_name}. Always refer to yourself by this name. "
        "Always respect saved user preferences."
    ),
    sub_agents=[
        developer_agent,
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
        connect_to_platform,
        check_connection,
        report_health,
        check_upstream,
        analyze_upstream_file,
        remember_info,
        search_memory,
        recall_human_preferences,
        recall_technical_context,
        google.adk.tools.FunctionTool(schedule_system_task, require_confirmation=True),
        google.adk.tools.FunctionTool(schedule_recurring_system_task, require_confirmation=True),
        google.adk.tools.FunctionTool(update_self, require_confirmation=True),
        google.adk.tools.FunctionTool(session_refresh, require_confirmation=True),
        google.adk.tools.FunctionTool(trigger_rollback, require_confirmation=True),
        google.adk.tools.FunctionTool(set_planner_mode, require_confirmation=True),
        save_user_preferences,
        get_user_preferences,
        google_search_agent_tool,
        web_fetch,
    ],
    before_agent_callback=[state_setter],
    before_model_callback=prompt_injection_guardrail,
    before_tool_callback=admin_tool_guardrail,
    after_tool_callback=tool_output_injection_guardrail,
    planner=BuiltInPlanner(
        thinking_config=types.ThinkingConfig(
            include_thoughts=True, thinking_budget=-1
        )
    ),
)
