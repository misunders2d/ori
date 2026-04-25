"""Agent-facing planner tools — thin async wrappers over `app.runtime.plan_storage`.

The storage layer (SQLite-backed, durable across restarts — Phase B) is the
single source of truth. Tools just translate between the agent's calling
convention and the storage API.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext

from app.plugins._common import state_session_id
from app.runtime import plan_storage

logger = logging.getLogger(__name__)


def _session_id(tool_context: ToolContext) -> str | None:
    return state_session_id(tool_context) if tool_context else None


async def create_plan(
    task: str,
    steps: list[str],
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Create a multi-step plan for the current session.

    Fails if there is already an active plan in this session — abandon it
    first via `abandon_plan`.
    """
    sid = _session_id(tool_context)
    if not sid:
        return {"status": "error", "message": "create_plan requires a session_id"}
    if not steps:
        return {"status": "error", "message": "create_plan requires at least one step"}
    return await plan_storage.create_plan(sid, task, steps)


async def get_next_step(tool_context: ToolContext = None) -> dict[str, Any]:
    """Claim the next pending step. If a step is already in_progress (e.g.
    after a crash mid-step), return that one — do not double-claim."""
    sid = _session_id(tool_context)
    if not sid:
        return {"status": "error", "message": "get_next_step requires a session_id"}
    step = await plan_storage.get_next_step(sid)
    if step is None:
        return {"status": "success", "message": "No pending steps. Plan is complete or absent."}
    return {
        "status": "success",
        "step_index": step["step_index"],
        "description": step["description"],
        "instruction": (
            f"Execute step {step['step_index']}: {step['description']}. "
            "When done, call complete_step(result=<your-summary>)."
        ),
    }


async def complete_step(result: str, tool_context: ToolContext = None) -> dict[str, Any]:
    """Mark the current in_progress step as done. Returns the next step (if
    any) or 'All steps completed!' when the plan is finished."""
    sid = _session_id(tool_context)
    if not sid:
        return {"status": "error", "message": "complete_step requires a session_id"}
    return await plan_storage.complete_step(sid, result)


async def abandon_plan(tool_context: ToolContext = None) -> dict[str, Any]:
    """Mark the active plan as abandoned. Use when an unrecoverable error
    means the plan can't continue."""
    sid = _session_id(tool_context)
    if not sid:
        return {"status": "error", "message": "abandon_plan requires a session_id"}
    changed = await plan_storage.abandon_plan(sid)
    return {
        "status": "success",
        "message": "Plan abandoned." if changed else "No active plan to abandon.",
    }


async def get_plan_status(tool_context: ToolContext = None) -> dict[str, Any]:
    """Full status of the current session's plan: task, status, steps, results."""
    sid = _session_id(tool_context)
    if not sid:
        return {"status": "error", "message": "get_plan_status requires a session_id"}
    plan = await plan_storage.get_plan_status(sid)
    if plan is None:
        return {"status": "success", "message": "No plan exists for this session."}
    return {"status": "success", "plan": plan}


# Convenience used by the scheduler — invoked OUTSIDE a tool context, so
# takes session_id directly.
async def seed_plan_for_session(session_id: str, task: str, steps: list[str]) -> None:
    await plan_storage.seed_plan(session_id, task, steps)


# Re-exports used by callers that don't have a tool_context but do have a
# session_id (the workflow's completion-check node, the scheduler driver).
async def has_pending_steps_for(session_id: str) -> bool:
    return await plan_storage.has_pending_steps(session_id)


async def get_active_plan_context_for(session_id: str) -> str | None:
    return await plan_storage.get_active_plan_context(session_id)
