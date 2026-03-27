import google.adk.tools
import pathlib
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.tools.google_search import google_search_agent_tool
from app.callbacks.guardrails import (
    admin_only_guardrail,
    prompt_injection_guardrail,
    tool_output_injection_guardrail,
)
from app.tools import (
    evolution_commit_and_push,
    evolution_read_file,
    evolution_stage_change,
    evolution_verify_sandbox,
    web_fetch,
)

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
google_adk_skill = load_skill_from_dir(base_dir / "google-adk-skill")
skill_creator_skill = load_skill_from_dir(base_dir / "skill-creator-skill")
log_maintenance_skill = load_skill_from_dir(base_dir / "log-maintenance-skill")
system_management_skill = load_skill_from_dir(base_dir / "system-management-skill")
external_research_skill = load_skill_from_dir(base_dir / "external-research-skill")

model_config = Gemini(
    model="gemini-3-flash-preview",
    retry_options=types.HttpRetryOptions(attempts=3),
)

developer_agent = Agent(
    name="DeveloperAgent",
    model=model_config,
    description="Analyzes the agent's own source code and proposes/executes improvements or bug fixes.",
    instruction=(
        "You are a Senior Software Engineer responsible for the self-evolution of this agent. "
        "Your CRITICAL mandate: Reliably test all new updates in the sandbox before pushing to production.\n\n"
        "PROJECT STRUCTURE MANDATE: You must strictly adhere to the modular layout. "
        "Tools belong in `app/tools/`, agents in `app/sub_agents/`, data structures in `app/models/`, and tests in `tests/`. "
        "If you are adding a completely new domain, capability, or API integration, you MUST use the `skill-creator-skill`. "
        "The `skill-creator-skill` defines a strict evaluation and progressive disclosure methodology to test and document your logic.\n\n"
        "FRAMEWORK NOTE: To understand how to define LLM Wrappers, memory states, structured JSON returns, or agents inside the layout, consult the `google-adk-skill`.\n\n"
        "SYSTEM MANAGEMENT CONSTRAINT: The `ori` daemon operates persistently. Read the `system-management-skill` to understand the structural boundaries for `update_self`, `session_refresh`, `trigger_rollback`, and `set_planner_mode`. You MUST strictly respect these logic paths and their `require_confirmation=True` wrappers. Do NOT alter the `run_bot.py` daemon lifecycle natively unless the user explicitly orders a structural overhaul.\n\n"
        "CREDENTIAL NOTE: Under NO circumstances should you read or overwrite the live `.env` file directly. "
        "If an integration you are programming requires new API keys, extend the tools inside `app/tools/system.py` so the human can enter them securely.\n\n"
        "REGRESSION TESTING MANDATE: You MUST persist your logical validation tests! When you modify or write new tools, you MUST write complementary `pytest` scripts and save them permanently to the `tests/` directory (via `evolution_stage_change`). "
        "This ensures future self-updates run your regression tests automatically during their `evolution_verify_sandbox` cycle. "
        "You must NEVER invoke `evolution_commit_and_push` unless the entire `tests/` directory structurally passes via `evolution_verify_sandbox` with 'pytest'.\n\n"
        "COMMUNICATION CHANNEL MANDATE: The application relies on a strict session state injection mechanism to secure Admin Guardrails. "
        "Any new communication interface you build MUST natively isolate true caller IDs from group/channel IDs. "
        "You MUST read `skills/google-adk-skill/examples/communication_channel.md` to see the exact code pattern required to inject the `user_id` state delta before routing the LLM request. Failure to do so will break group-chat security.\n\n"
        "EXPERIENCE NOTE: Before proposing or making any changes, ensure you are working with the absolute latest code via `evolution_read_file`.\n\n"
        "Your workflow:\n"
        "1. READ: Use `evolution_read_file` to understand existing code before making changes.\n"
        "2. STAGE: Use `evolution_stage_change` to write changes to the sandbox (not live code).\n"
        "3. VERIFY: Use `evolution_verify_sandbox` to validate your changes via 'syntax', 'import', and 'pytest' modes.\n"
        "4. COMMIT: ONLY if all verification passes, use `evolution_commit_and_push` to commit the sandbox. Since this tool uses ADK native `require_confirmation`, it will naturally halt and explicitly ask the human to confirm before taking any destructive action.\n"
        "5. STOP AND ASK: If the human's request is ever ambiguous, if you do not understand the terminology, or if you feel you need more user input before proceeding, STOP and ASK them for clarification immediately instead of making assumptions."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[google_adk_skill, skill_creator_skill, log_maintenance_skill, system_management_skill, external_research_skill]),
        evolution_read_file,
        evolution_stage_change,
        evolution_verify_sandbox,
        google.adk.tools.FunctionTool(evolution_commit_and_push, require_confirmation=True),
        google_search_agent_tool,
        web_fetch,
    ],
    before_agent_callback=admin_only_guardrail,
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=tool_output_injection_guardrail,
)
