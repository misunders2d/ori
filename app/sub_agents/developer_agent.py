import re

import google.adk.tools
import pathlib
from typing import AsyncGenerator
from google.adk.agents import Agent, BaseAgent, LoopAgent
from google.adk.models import Gemini
from google.genai import types
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.events import Event, EventActions
from google.adk.agents.invocation_context import InvocationContext

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
    evolution_read_sandbox_file,
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

# --- 1. Generator Agent (Formerly DeveloperAgent) ---

generator_agent = Agent(
    name="GeneratorAgent",
    model=model_config,
    description="The primary coding unit. Analyzes requirements and implements code changes in the sandbox.",
    instruction=(
        "You are a Senior Software Engineer responsible for implementing code changes.\n\n"
        "Your workflow:\n"
        "1. READ: Use `evolution_read_file` to understand existing code.\n"
        "2. STAGE: Use `evolution_stage_change` to write changes to the sandbox.\n"
        "3. VERIFY: Use `evolution_verify_sandbox` ('syntax', 'import', and 'pytest').\n"
        "4. ITERATE: If verification fails, fix the code and stage again.\n"
        "5. STOP: Once you have a verified implementation that passes tests, stop and let the ReviewerAgent evaluate your work.\n\n"
        "CRITICAL: Do NOT call `evolution_commit_and_push` directly. Your job is to prepare the change in the sandbox."
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
        google_search_agent_tool,
        web_fetch,
    ],
    before_agent_callback=admin_only_guardrail,
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=tool_output_injection_guardrail,
)

# --- 2. Reviewer Agent (The Critic) ---

reviewer_agent = Agent(
    name="ReviewerAgent",
    model=model_config,
    description="Evaluates code changes in the sandbox for quality, safety, and adherence to standards.",
    instruction=(
        "You are a Principal Software Engineer and Security Auditor.\n\n"
        "Your job is to review the code changes staged in the sandbox by the GeneratorAgent.\n"
        "1. Use `evolution_read_sandbox_file` to inspect the staged files in the sandbox.\n"
        "2. Use `evolution_verify_sandbox` to independently confirm the code passes tests.\n"
        "3. Evaluate the code for:\n"
        "   - Safety: No removal of guardrails.\n"
        "   - Quality: Idiomatic Python, proper error handling, modularity.\n"
        "   - Correctness: Does it fulfill the original request?\n\n"
        "OUTPUT REQUIREMENT: You MUST conclude your review with a clear 'GRADE: PASS' or 'GRADE: FAIL'.\n"
        "If FAIL, provide specific, actionable feedback for the GeneratorAgent to fix."
    ),
    tools=[
        evolution_read_file,
        evolution_read_sandbox_file,
        evolution_verify_sandbox,
        analyze_upstream_file,
        recall_technical_context,
    ],
    before_agent_callback=admin_only_guardrail,
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=tool_output_injection_guardrail,
)

# --- 3. Review Checker (Logic Gate) ---

_GRADE_PASS_RE = re.compile(r'\bGRADE:\s*PASS\b', re.IGNORECASE)

class ReviewChecker(BaseAgent):
    """Inspects the ReviewerAgent's output and determines if the loop should terminate."""
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        # Inspect the conversation history for the Reviewer's grade
        found_pass = False
        for event in reversed(ctx.session.events):
            if event.author == "ReviewerAgent" and event.content and event.content.parts:
                text = event.content.parts[0].text
                if text and _GRADE_PASS_RE.search(text):
                    found_pass = True
                    break
                # Stop searching once we hit the most recent reviewer event
                break

        if found_pass:
            # Signal success via session state so the outer agent can check deterministically
            yield Event(
                author=self.name,
                actions=EventActions(
                    escalate=True,
                    state_delta={"review_passed": True},
                ),
            )
        else:
            # Signal failure — loop continues, but if this is the last iteration
            # the outer agent will see review_passed=False
            yield Event(
                author=self.name,
                actions=EventActions(state_delta={"review_passed": False}),
            )

# --- 4. Committer Agent (Final Action) ---

committer_agent = Agent(
    name="CommitterAgent",
    model=model_config,
    description="Finalizes the development process by committing and pushing verified changes.",
    instruction=(
        "You are the deployment coordinator. You only run after a successful code review.\n"
        "Your ONLY task is to call `evolution_commit_and_push` with a descriptive message.\n"
        "Do NOT modify any code. Just commit what is in the sandbox."
    ),
    tools=[
        google.adk.tools.FunctionTool(evolution_commit_and_push, require_confirmation=True),
    ],
    before_agent_callback=admin_only_guardrail,
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=tool_output_injection_guardrail,
)

# --- 5. Orchestrated Developer Agent (The System) ---

development_loop = LoopAgent(
    name="DevelopmentLoop",
    sub_agents=[
        generator_agent,
        reviewer_agent,
        ReviewChecker(name="ReviewChecker"),
    ],
    max_iterations=3,
)

# --- 6. Commit Gate (Deterministic Check) ---

class CommitGate(BaseAgent):
    """Checks session state for review_passed before allowing the CommitterAgent to run."""
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state or {}
        review_passed = state.get("review_passed", False)

        if not review_passed:
            yield Event(
                author=self.name,
                content=types.Content(parts=[types.Part(text=(
                    "Review did NOT pass after all iterations. "
                    "Aborting commit to protect code quality."
                ))]),
                actions=EventActions(escalate=True),
            )
        else:
            # Pass through — let the next sub_agent (CommitterAgent) run
            yield Event(author=self.name)

# Final exported agent
developer_agent = Agent(
    name="DeveloperAgent",
    description="Analyzes the agent's own source code and proposes/executes improvements or bug fixes using a generator-reviewer loop.",
    sub_agents=[
        development_loop,
        CommitGate(name="CommitGate"),
        committer_agent,
    ],
    instruction=(
        "You are the entry point for the self-evolution system.\n"
        "1. Start the `DevelopmentLoop` to implement and review the changes.\n"
        "2. The `CommitGate` will automatically check if the review passed.\n"
        "3. If the gate passes, `CommitterAgent` will push the changes.\n"
        "4. If the gate fails, report that the review did not pass and no changes were committed.\n\n"
        "CRITICAL: Never bypass the CommitGate. If the review loop exhausted all iterations without passing, do NOT commit."
    ),
    model=model_config,
)
