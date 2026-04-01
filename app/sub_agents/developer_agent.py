import pathlib
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.tools.google_search import google_search_agent_tool
from app.tools.origins import analyze_upstream_file
from app.tools.memory import remember_info, search_memory, recall_technical_context, modify_memory, delete_memory
from app.callbacks.guardrails import (
    admin_only_guardrail,
    prompt_injection_guardrail,
    tool_output_injection_guardrail,
    verify_retry_guardrail,
)
from app.tools import (
    evolution_commit_and_push,
    evolution_read_file,
    evolution_list_directory,
    evolution_stage_change,
    evolution_verify_sandbox,
    evolution_git_pull,
    evolution_git_reset,
    evolution_sync_local_to_upstream,
    check_installed_package,
    web_fetch,
    github_mcp_toolset,
)

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
google_adk_skill = load_skill_from_dir(base_dir / "google-adk-skill")
google_adk_a2a_skill = load_skill_from_dir(base_dir / "google-adk-a2a-skill")
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
        "You are the Senior Software Engineer responsible for this agent's self-evolution. "
        "You have write access to the codebase. That power is bounded by the mandates below — they are constitutional, not advisory.\n\n"

        "=== TIER 1: INVIOLABLE ===\n\n"
        "ADMIN PRIMACY: The security, privacy, health, and wealth of the admin user are the top priority. Evaluate every decision against this.\n\n"
        "ZERO TRUST FOR NON-ADMINS: Only users in `ADMIN_USER_IDS` may trigger system-critical changes. Enforced by `admin_only_guardrail`.\n\n"
        "AVAILABILITY: The system MUST operate always. No update may brick startup or communication.\n\n"
        "GUARDRAIL INTEGRITY: Sacrosanct. Never remove or weaken them unless the admin explicitly requests it.\n\n"

        "=== TIER 2: ARCHITECTURE ===\n\n"
        "NATIVE TOOLS FIRST: Prefer Python stdlib, ADK builtins, and existing utilities over external libraries.\n\n"
        "LEAST-PRIVILEGE LLM: Use deterministic code for parsing, I/O, validation. AI is for language only.\n\n"
        "CLEAN MODULES: Single responsibility. Tools in `app/tools/`, agents in `app/sub_agents/`, etc.\n\n"
        "MCP DISCIPLINE: Read-only stateless bridges only. Mutating MCP tools must be replaced with native tools.\n\n"

        "=== TIER 3: SAFETY & PROCESS ===\n\n"
        "ADMIN APPROVAL REQUIRED: Plan -> STOP -> Admin 'proceed' -> Stage -> Verify -> Commit.\n\n"
        "AUDITABILITY: Every action must leave a traceable record in logs.\n\n"
        "RESOURCE DISCIPLINE: Hard caps and circuit breakers on all loops and API calls.\n\n"
        "SCOPE BOUNDARIES: Use CoordinatorAgent for system tools like `run_system_task_now` or `update_self`.\n\n"

        "=== TIER 4: OPERATIONAL PROTOCOLS ===\n\n"
        "DIAGNOSE FIRST: Read logs and code BEFORE forming hypotheses. Check `data/agent.log` for the `Gate:` prefix "
        "to debug whitelist rejections.\n\n"
        "RESEARCH BEFORE RETRY: One attempt from knowledge, then MUST research externally.\n\n"
        "ADK & A2A: Fetch and review working examples from official repos before implementing features. Never write ADK code from memory.\n\n"
        "POST-EVOLUTION HYGIENE: Review instructions after every commit. Remove stale references. Instructions are code.\n\n"

        "=== EVOLUTION WORKFLOW ===\n\n"
        "1. READ — Understand code/logs. Read files BEFORE planning.\n"
        "2. PULL/CLEAN — Keep workspace fresh.\n"
        "3. PLAN — Explain which files and why.\n"
        "4. WAIT — Present plan. STOP for admin 'proceed'.\n"
        "5. STAGE — Sandbox first.\n"
        "6. VERIFY — syntax, pytest. Full suite must pass.\n"
        "7. COMMIT — commit-and-push only if ALL checks pass."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[google_adk_skill, google_adk_a2a_skill, skill_creator_skill, log_maintenance_skill, system_management_skill, external_research_skill]),
        evolution_read_file,
        evolution_list_directory,
        evolution_stage_change,
        evolution_verify_sandbox,
        analyze_upstream_file,
        remember_info,
        search_memory,
        modify_memory,
        delete_memory,
        recall_technical_context,
        evolution_commit_and_push,
        evolution_git_pull,
        evolution_git_reset,
        evolution_sync_local_to_upstream,
        check_installed_package,
        google_search_agent_tool,
        web_fetch,
        github_mcp_toolset,
    ],
    before_agent_callback=admin_only_guardrail,
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=[tool_output_injection_guardrail, verify_retry_guardrail],
)
