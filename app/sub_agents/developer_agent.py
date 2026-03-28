import google.adk.tools
import pathlib
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.tools.google_search import google_search_agent_tool
from app.tools.origins import analyze_upstream_file
from app.tools.memory import remember_info, search_memory, recall_technical_context
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
        "Tools belong in `app/tools/`, agents in `app/sub_agents/`, data structures in `app/models/`, and tests in `tests/`.\n\n"
        "LONG-TERM MEMORY: Use `remember_info` to record bug fixes, architecture decisions, and important repos. "
        "Use `recall_technical_context` to search for past solutions when facing similar coding tasks. "
        "This ensures our evolution is consistent and doesn't repeat past mistakes.\n\n"
        "SYSTEM MANAGEMENT CONSTRAINT: Read the `system-management-skill`. You MUST respect `require_confirmation=True` wrappers.\n\n"
        "REGRESSION TESTING MANDATE: You MUST persist your logical validation tests in the `tests/` directory.\n\n"
        "Your workflow:\n"
        "1. READ: Use `evolution_read_file` to understand existing code.\n"
        "2. STAGE: Use `evolution_stage_change` to write changes to the sandbox.\n"
        "3. VERIFY: Use `evolution_verify_sandbox` ('syntax', 'import', and 'pytest').\n"
        "4. COMMIT: ONLY if all verification passes, use `evolution_commit_and_push`."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[google_adk_skill, skill_creator_skill, log_maintenance_skill, system_management_skill, external_research_skill]),
        evolution_read_file,
        evolution_stage_change,
        evolution_verify_sandbox,
        analyze_upstream_file,
        remember_info,
        search_memory,
        recall_technical_context,
        google.adk.tools.FunctionTool(evolution_commit_and_push, require_confirmation=True),
        google_search_agent_tool,
        web_fetch,
    ],
    before_agent_callback=admin_only_guardrail,
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=tool_output_injection_guardrail,
)
