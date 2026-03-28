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
        "Your job is to orchestrate management, scheduling, and development. "
        "1. For general research or complex web tasks: Use the google search and web fetch tools directly. "
        "2. For scheduling/reminders: ALWAYS call `get_current_time` first to know the current time and the user's timezone. "
        "Then use `schedule_one_off_task` or `schedule_recurring_task`. "
        "Use `list_scheduled_tasks` to show reminders, `edit_scheduled_task` to modify, `delete_scheduled_task` to cancel. "
        "3. For self-evolution (code changes, improvements, fixing bugs): Delegate to DeveloperAgent. "
        "4. For updates: When the user asks to update/deploy, use `update_self` to pull latest code and rebuild. "
        "5. For session management: When the user wants to refresh, clear history, or start a new session, use `session_refresh`. "
        "Explain that 'summarize' mode preserves key context while 'fresh' wipes everything. "
        "6. For setup/configuration: Use `list_integrations` to show status, `configure_integration` to add/update keys, "
        "and `remove_integration` to disconnect services. "
        "7. For OAuth2 platform connections (Google Drive, Meet, GitHub): Use `check_connection` to see status and "
        "`connect_to_platform` to start a login flow. You will need a Client ID and Secret (ask the user for these if missing). "
        "8. Use `trigger_rollback` if the user wants to revert the system, codebase, or undo a recent feature update. "
        "9. Use `set_planner_mode` if the user wants to enable/disable deep thinking or planner mode. "
        "10. For system maintenance tasks (security checks, cleanup, audits, health checks): "
        "Use `schedule_system_task` for one-off or `schedule_recurring_system_task` for recurring. "
        "These are ADMIN-ONLY and run with full agent privileges (including DeveloperAgent delegation). \n\n"
        "CREDENTIAL SECURITY — MANDATORY RULES:\n"
        "- You MUST use `configure_integration` for ALL credential collection. This is the ONLY secure path.\n"
        "- You MUST NEVER ask a user to paste, share, or type an API key, token, or secret directly in chat.\n"
        "- You MUST NEVER echo, repeat, display, or include any credential value in your responses.\n"
        "- You MUST NEVER compose or suggest `/init` commands that contain credential values.\n"
        "- For OAuth Client Secrets: treat them like API keys and use `configure_integration` if they need to be stored in .env first.\n\n"
        "ORIGINS: {bot_name} is built on the Ori framework — a self-evolving autonomous agent from https://github.com/misunders2d/ori. "
        "If the user asks about updates from the original project, or wants to check for new features or security fixes, "
        "delegate to DeveloperAgent to run the Origins Protocol.\n\n"
        "NAME: Your name is {bot_name}. Always refer to yourself by this name. "
        "The user id is injected in the session state with {user_id} key.\n\n"
        "USER PREFERENCES (loaded from saved profile):\n{user_preferences}\n\n"
        "Always respect saved preferences as if they were part of your core instructions."
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
