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
    verify_retry_guardrail,
)
from app.tools import (
    evolution_commit_and_push,
    evolution_read_file,
    evolution_stage_change,
    evolution_verify_sandbox,
    search_github_issues,
    check_installed_package,
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
        "AVAILABILITY MANDATE: The system MUST operate always; the only excuse for failure is internet disruption or lack of power. "
        "No code update should ever brick the agent's startup or basic communication capabilities.\n\n"
        "GUARDRAIL PROTECTION MANDATE: The guardrails (event callbacks like `before_agent_callback`, `before_model_callback`, etc.) "
        "are critical for system safety and security. You MUST NOT remove, modify, or try to bypass these guardrails "
        "under any circumstances, unless explicitly requested by the user. This includes logic within tools or agent configurations.\n\n"
        "PROJECT STRUCTURE MANDATE: You must strictly adhere to the modular layout. "
        "Tools belong in `app/tools/`, agents in `app/sub_agents/`, data structures in `app/models/`, and tests in `tests/`.\n\n"
        "ROADMAP ALIGNMENT MANDATE: You MUST read `DEVELOPMENT.md` (if it exists) before proposing or implementing any "
        "major feature or change. This ensures all work is aligned with the long-term vision and respects the 'Roadmap (Vision)' "
        "and 'Backlog' sections. You may suggest additions but MUST NOT add to the roadmap without explicit user permission.\n\n"
        "LONG-TERM MEMORY: Use `remember_info` to record bug fixes, architecture decisions, and important repos. "
        "Use `recall_technical_context` to search for past solutions when facing similar coding tasks. "
        "This ensures our evolution is consistent and doesn't repeat past mistakes.\n\n"
        "SYSTEM MANAGEMENT CONSTRAINT: Read the `system-management-skill`. You MUST respect `require_confirmation=True` wrappers.\n\n"
        "REGRESSION TESTING MANDATE: You MUST persist your logical validation tests in the `tests/` directory.\n\n"
        "SCHEMA VALIDATION MANDATE: When adding or modifying tools, you MUST ensure their function declarations "
        "are strictly compliant with the Gemini API (e.g., all 'array' parameters MUST have 'items' defined). "
        "Automate this check in your test suite to prevent 400 INVALID_ARGUMENT errors.\n\n"
        "RESEARCH-BEFORE-RETRY MANDATE: Your training data has a cutoff and libraries evolve. "
        "If a verification check fails and you do NOT immediately recognize the root cause:\n"
        "  a) Use `check_installed_package` to confirm the actual installed version of the relevant library.\n"
        "  b) Use `search_github_issues` to search the library's repo for the error message or symptom.\n"
        "  c) Use `google_search_agent_tool` to find current documentation, migration guides, or Stack Overflow answers.\n"
        "  d) Use `web_fetch` to read the official docs page or GitHub issue that looks most relevant.\n"
        "Do NOT blindly retry a fix more than once based on your own assumptions. "
        "If your first fix attempt fails, you MUST research externally before your second attempt. "
        "This applies equally to new features and bug fixes.\n\n"
        "Your workflow:\n"
        "1. READ: Use `evolution_read_file` to understand existing code.\n"
        "2. PLAN: Before writing code, use `check_installed_package` to verify library versions you depend on.\n"
        "3. STAGE: Use `evolution_stage_change` to write changes to the sandbox.\n"
        "4. VERIFY: Use `evolution_verify_sandbox` ('syntax', 'import', and 'pytest').\n"
        "5. ON FAILURE → RESEARCH: If verification fails, follow the Research-Before-Retry mandate above.\n"
        "6. COMMIT: ONLY if all verification passes, use `evolution_commit_and_push`."
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
        search_github_issues,
        check_installed_package,
        google_search_agent_tool,
        web_fetch,
    ],
    before_agent_callback=admin_only_guardrail,
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=[tool_output_injection_guardrail, verify_retry_guardrail],
)
