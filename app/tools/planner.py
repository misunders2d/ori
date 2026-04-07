"""Structured plan-and-execute system for complex multi-step tasks.

Creates sequential plans with enforcement — the agent can only see and
execute the current step. A before_model_callback injects plan context
to prevent drift.
"""

import json
import logging
import os
import time
from typing import Optional

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_PLANS_DIR = os.path.abspath("./tmp/plans")


def _plan_path(session_id: str) -> str:
    return os.path.join(_PLANS_DIR, f"{session_id}.json")


def _load_plan(session_id: str) -> dict | None:
    path = _plan_path(session_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_plan(session_id: str, plan: dict):
    os.makedirs(_PLANS_DIR, exist_ok=True)
    with open(_plan_path(session_id), "w") as f:
        json.dump(plan, f, indent=2)


def _get_session_id(tool_context: ToolContext) -> str:
    session = getattr(tool_context, "session", None)
    return getattr(session, "session_id", None) or getattr(session, "id", None) or "default"


def create_plan(
    task_description: str,
    steps: list[str],
    tool_context: ToolContext,
) -> dict:
    """Create a structured execution plan for a complex task.

    Break the task into sequential steps. Once created, you MUST execute
    steps one at a time using get_next_step and complete_step.

    Args:
        task_description: High-level description of what you're trying to accomplish.
        steps: Ordered list of step descriptions (e.g. ["Fetch sales data from BigQuery", "Generate chart", "Post to Slack"]).

    Returns:
        dict with the plan overview.
    """
    if not steps:
        return {"status": "error", "message": "Plan must have at least one step."}

    session_id = _get_session_id(tool_context)

    # Check for existing active plan
    existing = _load_plan(session_id)
    if existing and existing.get("status") == "active":
        pending = sum(1 for s in existing["steps"] if s["status"] == "pending")
        return {
            "status": "error",
            "message": f"A plan is already active with {pending} pending steps. "
                       "Complete or abandon it first (use abandon_plan).",
        }

    plan = {
        "task": task_description,
        "status": "active",
        "created_at": time.time(),
        "current_step": 0,
        "steps": [
            {"id": i, "description": desc, "status": "pending", "result": None}
            for i, desc in enumerate(steps)
        ],
    }
    _save_plan(session_id, plan)

    return {
        "status": "success",
        "message": f"Plan created with {len(steps)} steps. Use get_next_step to begin.",
        "plan": {
            "task": task_description,
            "total_steps": len(steps),
            "steps": [f"[ ] {s}" for s in steps],
        },
    }


def get_next_step(tool_context: ToolContext) -> dict:
    """Get the next pending step in the active plan.

    Returns ONLY the current step — you must complete it before moving on.
    Do not skip ahead or work on multiple steps at once.

    Returns:
        dict with the current step details, or completion status if all done.
    """
    session_id = _get_session_id(tool_context)
    plan = _load_plan(session_id)

    if not plan or plan.get("status") != "active":
        return {"status": "no_plan", "message": "No active plan. Create one with create_plan."}

    for step in plan["steps"]:
        if step["status"] == "pending":
            step["status"] = "in_progress"
            plan["current_step"] = step["id"]
            _save_plan(session_id, plan)

            completed = sum(1 for s in plan["steps"] if s["status"] == "done")
            total = len(plan["steps"])

            return {
                "status": "success",
                "task": plan["task"],
                "progress": f"{completed}/{total}",
                "current_step": {
                    "id": step["id"],
                    "description": step["description"],
                },
                "instruction": f"Execute ONLY this step: {step['description']}. "
                               "When done, call complete_step with the result.",
            }

    # All steps done
    plan["status"] = "completed"
    _save_plan(session_id, plan)
    return {
        "status": "plan_complete",
        "message": "All steps completed!",
        "results": [
            {"step": s["description"], "result": s["result"]}
            for s in plan["steps"]
        ],
    }


def complete_step(
    result: str,
    tool_context: ToolContext,
) -> dict:
    """Mark the current step as done and record its result.

    Args:
        result: Brief summary of what was accomplished in this step.

    Returns:
        dict with updated progress and the next step preview.
    """
    session_id = _get_session_id(tool_context)
    plan = _load_plan(session_id)

    if not plan or plan.get("status") != "active":
        return {"status": "error", "message": "No active plan."}

    current_id = plan.get("current_step", 0)
    step = plan["steps"][current_id]

    if step["status"] != "in_progress":
        return {"status": "error", "message": f"Step {current_id} is not in progress (status: {step['status']})."}

    step["status"] = "done"
    step["result"] = result

    _save_plan(session_id, plan)

    completed = sum(1 for s in plan["steps"] if s["status"] == "done")
    total = len(plan["steps"])

    # Preview next step
    next_step = None
    for s in plan["steps"]:
        if s["status"] == "pending":
            next_step = s["description"]
            break

    response = {
        "status": "success",
        "progress": f"{completed}/{total}",
        "completed_step": step["description"],
    }

    if next_step:
        response["next_step_preview"] = next_step
        response["instruction"] = "Call get_next_step to proceed."
    else:
        plan["status"] = "completed"
        _save_plan(session_id, plan)
        response["message"] = "All steps completed! Plan finished."
        response["results"] = [
            {"step": s["description"], "result": s["result"]}
            for s in plan["steps"]
        ]

    return response


def get_plan_status(tool_context: ToolContext) -> dict:
    """Show the full plan with current progress and checkboxes.

    Returns:
        dict with the complete plan status.
    """
    session_id = _get_session_id(tool_context)
    plan = _load_plan(session_id)

    if not plan:
        return {"status": "no_plan", "message": "No active plan."}

    checklist = []
    for s in plan["steps"]:
        if s["status"] == "done":
            checklist.append(f"[x] {s['description']} — {s['result']}")
        elif s["status"] == "in_progress":
            checklist.append(f"[>] {s['description']} (in progress)")
        else:
            checklist.append(f"[ ] {s['description']}")

    completed = sum(1 for s in plan["steps"] if s["status"] == "done")
    total = len(plan["steps"])

    return {
        "status": "success",
        "task": plan["task"],
        "plan_status": plan["status"],
        "progress": f"{completed}/{total}",
        "checklist": checklist,
    }


def abandon_plan(tool_context: ToolContext) -> dict:
    """Abandon the current active plan. Use when the task is no longer needed.

    Returns:
        dict confirming abandonment.
    """
    session_id = _get_session_id(tool_context)
    plan = _load_plan(session_id)

    if not plan or plan.get("status") != "active":
        return {"status": "no_plan", "message": "No active plan to abandon."}

    plan["status"] = "abandoned"
    _save_plan(session_id, plan)

    completed = sum(1 for s in plan["steps"] if s["status"] == "done")
    total = len(plan["steps"])

    return {
        "status": "success",
        "message": f"Plan abandoned ({completed}/{total} steps were completed).",
    }


def get_active_plan_context(session_id: str) -> str | None:
    """Called by the before_model_callback to inject plan context.

    Returns a context string if a plan is active, None otherwise.
    Not a tool — internal use only.
    """
    plan = _load_plan(session_id)
    if not plan or plan.get("status") != "active":
        return None

    current = None
    for s in plan["steps"]:
        if s["status"] == "in_progress":
            current = s
            break

    completed = sum(1 for s in plan["steps"] if s["status"] == "done")
    total = len(plan["steps"])

    if current:
        return (
            f"[ACTIVE PLAN: {plan['task']}] "
            f"Progress: {completed}/{total}. "
            f"CURRENT STEP ({current['id']}): {current['description']}. "
            "Focus ONLY on this step. When done, call complete_step with the result."
        )
    else:
        return (
            f"[ACTIVE PLAN: {plan['task']}] "
            f"Progress: {completed}/{total}. "
            "No step in progress. Call get_next_step to continue."
        )
