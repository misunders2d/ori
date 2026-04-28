"""Worker + Judge pair for deterministic per-step plan execution.

Why two agents instead of one:

The naive ADK 2.0 single-agent pattern (mode='task' + output_schema +
tools) does not actually let the model call its tools — empirically
observed with `gemini-3.1-flash-lite-preview` and likely true for
other small models. The output_schema constraint forces the model to
produce the schema directly, skipping the tool-call cycle. We tried
this and saw 6 fabricated identical summaries per fire.

Why not just use the coordinator for per-step turns:

Coordinator is `mode='chat'`. ADK 2.0 hard-requires workflow-node
agents to be `mode='task'` or `mode='single_turn'`. A chat-mode agent
invoked via `ctx.run_node` returns None (proven by WORKER_SHAPE
type=NoneType in production logs). Coordinator stays mode='chat' for
normal user-facing chat; this module provides a task-mode counterpart
for the workflow.

The reliable pattern is the worker/judge split:

- `step_worker` (mode='task', FULL coordinator toolkit, NO
  output_schema) does the actual step's work. Returns free text
  describing what tools it called and what they returned.

- `step_judge` (mode='task', NO tools, output_schema=StepResult) reads
  the worker's free-text and classifies it into a typed pass/fail
  result. Skeptical by default: requires concrete tool-confirmed
  evidence to return status='completed'.

Sub-agents owned by the coordinator are exposed here as AgentTool
wrappers (parent-ownership constraint — can't redeclare them as
sub_agents under a second parent).
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
    """Typed pass/fail output from one plan step (produced by step_judge).

    The plan_executor workflow reads `status` to decide whether to advance
    or abandon. `summary` and `failure_reason` carry human-readable detail
    surfaced back to the user / logged.
    """

    status: Literal["completed", "failed"] = Field(
        description=(
            "'completed' ONLY if the worker's result shows concrete "
            "tool-confirmed evidence of the step's actual goal being "
            "achieved (BigQuery returned rows, sheet write returned "
            "updated_cells>0, file ID confirmed, etc.). 'failed' if any "
            "tool errored, evidence is missing, the worker returned a "
            "generic/hollow summary, or the worker hallucinated success."
        ),
    )
    summary: str = Field(
        description=(
            "One- to three-sentence description of what the worker "
            "actually did, drawn directly from the worker's report. "
            "Quote concrete numbers / IDs / sheet ranges when present."
        ),
    )
    failure_reason: str | None = Field(
        default=None,
        description=(
            "Required when status='failed'. The actual error / blocker "
            "in plain language — quote the tool's error message verbatim "
            "if present, or describe specifically what evidence was "
            "missing. Null when status='completed'."
        ),
    )


_WORKER_INSTRUCTION = (
    "You execute ONE plan step. Your input is `PLAN STEP N: <description>`. "
    "Do EXACTLY what the step says — no more, no less.\n\n"

    "Use the available tools and agent-tools to actually perform the step. "
    "Make REAL tool calls — do not narrate what you would do or summarize "
    "without running anything. After the tool calls return, report what "
    "actually happened: which tool(s) you called, what they returned (quote "
    "concrete numbers, IDs, sheet ranges, error messages), and the final "
    "outcome.\n\n"

    "RULES:\n"
    "- NEVER improvise alternative approaches if a tool errors. Report the "
    "  error verbatim and stop.\n"
    "- NEVER claim a write/delivery succeeded unless the tool result "
    "  confirmed it (updated_cells > 0, message_id present, file ID "
    "  returned, etc.).\n"
    "- NEVER fabricate generic summaries like 'analysis complete'. Only "
    "  report what tools actually returned.\n"
    "- Do NOT discuss the next step or summarize the whole plan."
)


_JUDGE_INSTRUCTION = (
    "You judge whether a plan step was actually completed. Your input is:\n\n"
    "  STEP: <the original step description>\n"
    "  WORKER_RESULT: <what the worker reported back>\n\n"

    "Return a StepResult. Be SKEPTICAL — generic prose is not evidence.\n\n"

    "status='completed' requires CONCRETE tool-confirmed evidence in "
    "WORKER_RESULT that the step's actual goal happened — e.g.:\n"
    "  - BigQuery query: row counts, sample values, or a saved CSV path\n"
    "  - Sheet write: updated_cells > 0, the actual range written, file ID\n"
    "  - SP-API call: order count / SKU count / inventory number returned\n"
    "  - Message delivery: message_id, channel name confirmed\n"
    "  - Keepa lookup: ASIN-level numbers (BSR, monthly sold, price)\n\n"

    "status='failed' if ANY of these apply to WORKER_RESULT:\n"
    "  - Mentions a tool error, exception, 401/403/404, or 'could not'\n"
    "  - Generic prose with no concrete numbers / IDs (the worker likely "
    "    didn't actually call tools)\n"
    "  - Same summary repeated regardless of step (clear hallucination)\n"
    "  - Empty / very short / placeholder content\n"
    "  - Claims success but the evidence required by the STEP description "
    "    isn't present (e.g., step says 'write to sheet X' but WORKER_RESULT "
    "    doesn't mention writing to sheet X with a confirmation)\n\n"

    "summary: 1-2 sentences quoting the concrete result from WORKER_RESULT.\n"
    "failure_reason: required for failed; describe specifically what evidence "
    "was missing or what error was reported."
)


# Sub-agents owned by the coordinator, exposed here as tools to bypass
# ADK's parent-ownership constraint without duplicating the agent tree.
_AGENT_TOOLS = [
    AgentTool(agent=amazon_head_agent),
    AgentTool(agent=knowledge_agent),
    AgentTool(agent=developer_agent),
    *([AgentTool(agent=clickup_agent)] if clickup_agent else []),
]

_WORKER_TOOLS = [
    *_AGENT_TOOLS,
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
]


step_worker = LlmAgent(
    name="StepWorker",
    mode="task",
    description=(
        "Executes a single plan step using full coordinator toolkit. "
        "task-mode (required for workflow-node compatibility), no "
        "output_schema (would block tool calls in practice). Returns "
        "free-text describing what tools were called and what they "
        "returned."
    ),
    model=get_model("StepWorker"),
    instruction=_WORKER_INSTRUCTION,
    tools=_WORKER_TOOLS,
)


step_judge = LlmAgent(
    name="StepJudge",
    mode="task",
    description=(
        "Reads a plan step's description and the worker's free-text "
        "result, returns typed StepResult{status, summary, failure_reason}. "
        "No tools — pure classification."
    ),
    model=get_model("StepJudge"),
    instruction=_JUDGE_INSTRUCTION,
    output_schema=StepResult,
)
