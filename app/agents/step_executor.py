"""StepExecutor — task-mode agent that runs one plan step.

Used by `app/workflows/plan_executor.py` as the deterministic per-step
runner for both ad-hoc and scheduled plans. Returns a typed `StepResult`
so the workflow can route on success/failure without parsing free text.

Why a separate agent (not just the coordinator):

- Per ADK 2.0 docs, agents embedded as workflow nodes must run in
  `task` or `single_turn` mode. The coordinator is `mode='chat'` for
  user-facing turns, so we need a dedicated task-mode agent for
  workflow-driven step execution.
- The coordinator already owns `developer_agent`, `knowledge_agent`,
  `amazon_head_agent`, etc. as `sub_agents`. ADK's parent-ownership
  constraint means we can't redeclare them under a second parent.
  Instead, this agent exposes them via `AgentTool(...)` wrappers, which
  is a tool registration (no parent conflict) — the wrapped agents stay
  owned by the coordinator.
- Tools are mirrored from the coordinator (re-instantiated where the
  toolset is class-based) so any kind of step can run: research, write,
  delivery, image gen, web fetch, scheduling, knowledge ops, etc. The
  user explicitly asked for full coverage rather than a curated subset
  — there's no way to predict what a user-defined plan step will need.
"""

from __future__ import annotations

import pathlib
from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import AgentTool, skill_toolset
from pydantic import BaseModel, Field

from app.agents.amazon_head_agent import amazon_head_agent
from app.agents.clickup_agent import clickup_agent
from app.agents.developer import developer_agent
from app.agents.knowledge import knowledge_agent
from app.tools.a2a import get_agent_identity, get_my_a2a_key, list_friends
from app.tools.google_search import google_search_agent_tool
from app.tools.model_tools import (
    get_llm_provider,
    list_available_models,
    switch_llm_provider,
)
from app.tools.web import web_fetch
from app.tools.whitelist import blacklist_chat, whitelist_chat
from app.toolsets import (
    CreativesToolset,
    IntegrationToolset,
    MemoryToolset,
    PlannerToolset,
    SchedulingToolset,
    ScratchpadToolset,
    SystemToolset,
)
from app.util.models import get_model

_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_scheduling_skill = load_skill_from_dir(_skills_dir / "scheduling-skill")
_configuration_skill = load_skill_from_dir(_skills_dir / "configuration-skill")
_approval_skill = load_skill_from_dir(_skills_dir / "approval-skill")
_scratchpad_skill = load_skill_from_dir(_skills_dir / "scratchpad-skill")
_spawn_skill = load_skill_from_dir(_skills_dir / "spawn-skill")
_model_swap_skill = load_skill_from_dir(_skills_dir / "model-swap-skill")


class StepResult(BaseModel):
    """Typed pass/fail output from one plan step.

    The workflow reads `status` to decide whether to advance the plan or
    abandon it. `summary` and `failure_reason` carry the human-readable
    detail that gets surfaced back to the user / logged.
    """

    status: Literal["completed", "failed"] = Field(
        description=(
            "'completed' only if the step's actual goal happened (write "
            "confirmed, data retrieved, message delivered). 'failed' if "
            "any tool errored, data is missing, you're blocked, or the "
            "outcome could not be confirmed."
        ),
    )
    summary: str = Field(
        description=(
            "One- to three-sentence description of what was done in this "
            "step. For 'completed', this is what the user/log will see; "
            "for 'failed', this is what was attempted before the failure."
        ),
    )
    failure_reason: str | None = Field(
        default=None,
        description=(
            "Required when status='failed'. The actual error / blocker "
            "in plain language — quote the tool's error message verbatim "
            "if the tool returned one. Null when status='completed'."
        ),
    )


_STEP_INSTRUCTION = (
    "You execute ONE plan step. Your input is a plan-step description; "
    "do exactly what it says, no more, no less.\n\n"

    "You have full agent capabilities available — research, knowledge "
    "graph, scheduling, integrations, image generation, web fetching, "
    "scratchpad, plus dedicated agents (Amazon ops, knowledge, developer, "
    "ClickUp) wrapped as agent-tools. Use whatever the step actually "
    "needs.\n\n"

    "When done, return a `StepResult`:\n"
    "- `status='completed'` only if the step's actual goal was achieved "
    "  (the spreadsheet WAS written and the tool confirmed it, the "
    "  report WAS delivered, the data WAS retrieved). The framework "
    "  parses your final answer into the schema.\n"
    "- `status='failed'` with a `failure_reason` if a tool errored, "
    "  data is missing, you're blocked, unauthorized, the action did "
    "  not actually happen, or the destination could not be confirmed.\n\n"

    "RULES:\n"
    "- NEVER improvise alternative approaches if a tool errors. Set "
    "  status='failed' with the actual error verbatim and stop.\n"
    "- NEVER claim a write/delivery succeeded unless the tool result "
    "  confirmed it (e.g. updated_cells > 0, message_id present, http "
    "  200 with the expected body).\n"
    "- NEVER fabricate system errors, 'tool re-sync' messages, "
    "  reconfiguration steps, or missing-skill alerts. If something is "
    "  unknown or missing, set status='failed' with a truthful reason.\n"
    "- Do NOT discuss the next step or summarize the whole plan — your "
    "  output is the result of THIS one step only."
)


step_executor = LlmAgent(
    name="StepExecutor",
    mode="task",
    description=(
        "Executes a single plan step with structured pass/fail output. "
        "Used by the plan_executor workflow for deterministic step "
        "execution; returns a typed StepResult."
    ),
    model=get_model("StepExecutor"),
    instruction=_STEP_INSTRUCTION,
    output_schema=StepResult,
    tools=[
        # Sub-agents owned by the coordinator are exposed here as tools
        # to sidestep ADK's parent-ownership constraint. AgentTool wraps
        # an agent as a callable; no parent-child relationship is
        # established.
        AgentTool(agent=amazon_head_agent),
        AgentTool(agent=knowledge_agent),
        AgentTool(agent=developer_agent),
        *([AgentTool(agent=clickup_agent)] if clickup_agent else []),
        # Mirror of coordinator's tool list. Toolsets are re-instantiated
        # (each toolset is a fresh wrapper around the underlying tool
        # functions). Skill instances are immutable and shared.
        skill_toolset.SkillToolset(skills=[
            _scheduling_skill,
            _configuration_skill,
            _approval_skill,
            _spawn_skill,
            _model_swap_skill,
            _scratchpad_skill,
        ]),
        SchedulingToolset(),
        MemoryToolset(),
        SystemToolset(),
        PlannerToolset(),
        IntegrationToolset(),
        ScratchpadToolset(),
        CreativesToolset(),
        *([google_search_agent_tool] if google_search_agent_tool else []),
        web_fetch,
        whitelist_chat,
        blacklist_chat,
        list_available_models,
        get_llm_provider,
        switch_llm_provider,
        get_agent_identity,
        get_my_a2a_key,
        list_friends,
    ],
)
