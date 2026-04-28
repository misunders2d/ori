"""StepJudge — typed pass/fail classifier for plan-step results.

The plan_executor workflow drives steps deterministically:

1. Per step, the workflow calls the coordinator with a per-step prompt
   (`PLAN STEP N: <description>`). Coordinator runs as it always does:
   uses its full toolkit, delegates to sub-agents, returns free-text
   describing what tools it called and what they returned.

2. The workflow then hands the step description + the coordinator's
   free-text to `step_judge` (this module). step_judge has
   `output_schema=StepResult` and no tools — pure classifier. It
   returns typed `StepResult{status, summary, failure_reason}`.

3. The workflow routes on `result.status`: `completed` records the
   summary and advances; `failed` records the reason and abandons.

Why a separate judge agent (and not a single agent with output_schema +
tools): empirically, mode='task' + output_schema + tools forces the
model to skip tool calls and produce the schema directly. Splitting
the work-doer (coordinator) from the result-classifier (judge) keeps
both halves working as intended.

Why hybrid coordinator + judge (and not a duplicate step_worker):
coordinator already has the full toolkit; spawning a parallel agent
tree just duplicated complexity. Coordinator stays the single source
of agent capability for both chat and plan-step turns.
"""

from __future__ import annotations

from typing import Literal

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from app.util.models import get_model


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


_JUDGE_INSTRUCTION = (
    "You judge whether a plan step was actually completed. Your input is:\n\n"
    "  STEP: <the original step description>\n"
    "  WORKER_RESULT: <what the worker (coordinator) reported back>\n\n"

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
    "  - Claims success but the evidence required by the STEP description "
    "    isn't present (e.g., step says 'write to sheet X' but WORKER_RESULT "
    "    doesn't mention writing to sheet X with a confirmation)\n\n"

    "summary: 1-2 sentences quoting the concrete result from WORKER_RESULT.\n"
    "failure_reason: required for failed; describe specifically what evidence "
    "was missing or what error was reported."
)


step_judge = LlmAgent(
    name="StepJudge",
    mode="task",
    description=(
        "Reads a plan step's description and the coordinator's free-text "
        "result, returns typed StepResult{status, summary, failure_reason}. "
        "No tools — pure classification."
    ),
    model=get_model("StepJudge"),
    instruction=_JUDGE_INSTRUCTION,
    output_schema=StepResult,
)
