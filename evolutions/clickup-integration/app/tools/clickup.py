"""ClickUp task management tools — async, API key auth.

Provides full CRUD for ClickUp tasks: list, create, update, comment,
plus team/space/folder discovery. All functions are async and use httpx.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

API_BASE = "https://api.clickup.com/api/v2"


def _headers() -> dict:
    token = os.environ.get("CLICKUP_API_TOKEN", "")
    return {"Authorization": token, "Content-Type": "application/json"}


def _is_configured() -> bool:
    return bool(os.environ.get("CLICKUP_API_TOKEN", "").strip())


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{API_BASE}{path}", headers=_headers(), params=params)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{API_BASE}{path}", headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()


async def _put(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(f"{API_BASE}{path}", headers=_headers(), json=payload)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------

async def clickup_get_workspace(tool_context: ToolContext) -> dict:
    """Get the ClickUp workspace: teams, members, and spaces with their statuses.

    Call this first to discover team IDs, space IDs, and member emails.
    The result is cached in session state for subsequent tool calls.

    Returns:
        dict with teams (id, name, members) and spaces (id, name, statuses).
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured. Set it via /init."}

    try:
        data = await _get("/team")
        teams = data.get("teams", [])

        user_email = ""
        state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
        user_email = state.get("user_id", "")

        workspace = {"user_email": user_email, "teams": [], "spaces": []}

        for team in teams:
            team_info = {
                "id": team["id"],
                "name": team["name"],
                "members": [
                    {"id": m["user"]["id"], "username": m["user"]["username"], "email": m["user"]["email"]}
                    for m in team.get("members", [])
                ],
            }
            workspace["teams"].append(team_info)

            space_data = await _get(f"/team/{team['id']}/space")
            for space in space_data.get("spaces", []):
                workspace["spaces"].append({
                    "id": space["id"],
                    "name": space["name"],
                    "statuses": [s.get("status", "") for s in space.get("statuses", [])],
                })

        tool_context.state["clickup_workspace"] = workspace
        return workspace
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def clickup_list_folders_and_lists(space_id: str) -> dict:
    """List all folders and lists within a ClickUp space.

    Args:
        space_id: The space ID (from clickup_get_workspace).

    Returns:
        dict with 'folders' and 'lists' arrays.
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured."}

    try:
        folders = await _get(f"/space/{space_id}/folder")
        lists = await _get(f"/space/{space_id}/list")
        return {
            "folders": folders.get("folders", []),
            "lists": lists.get("lists", []),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Task listing
# ---------------------------------------------------------------------------

async def clickup_list_tasks(
    team_id: str,
    tool_context: ToolContext,
    assignee_email: str | None = None,
    list_id: str | None = None,
    folder_id: str | None = None,
    status: str | None = None,
    due: str | None = None,
) -> dict:
    """List tasks with flexible filters.

    Args:
        team_id: The ClickUp team ID.
        assignee_email: Filter by assignee email. If empty, uses the current user's email.
        list_id: Restrict to a specific list. Omit to search the whole team.
        folder_id: Restrict to a specific folder. Omit to search the whole team.
        status: 'open', 'closed', or an exact ClickUp status name.
        due: 'today', 'tomorrow', 'week', or 'overdue'.

    Returns:
        dict with 'tasks' list on success.
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured."}

    # Resolve assignee email → ClickUp user ID
    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    email = assignee_email or state.get("user_id", "")
    if not email:
        return {"status": "error", "message": "No assignee email provided and no user_id in session."}

    user_id = await _resolve_user_id(email)
    if not user_id:
        return {"status": "error", "message": f"Could not find ClickUp user for {email}."}

    params: dict = {
        "assignees[]": user_id,
        "archived": "false",
        "subtasks": "true",
    }

    # Status filter
    if status:
        sl = status.lower()
        if sl == "open":
            params["include_closed"] = "false"
        elif sl == "closed":
            params["include_closed"] = "true"
        else:
            params["statuses[]"] = status

    # Due date filter
    now = datetime.now(timezone.utc)
    if due:
        dl = due.lower()
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        if dl == "today":
            params["due_date_gt"] = int(today_start.timestamp() * 1000)
            params["due_date_lt"] = int((today_start + timedelta(days=1)).timestamp() * 1000)
        elif dl == "tomorrow":
            params["due_date_gt"] = int((today_start + timedelta(days=1)).timestamp() * 1000)
            params["due_date_lt"] = int((today_start + timedelta(days=2)).timestamp() * 1000)
        elif dl in ("week", "next week"):
            params["due_date_gt"] = int(now.timestamp() * 1000)
            params["due_date_lt"] = int((now + timedelta(days=7)).timestamp() * 1000)
        elif dl == "overdue":
            params["due_date_lt"] = int(now.timestamp() * 1000)

    # Endpoint selection
    if list_id:
        path = f"/list/{list_id}/task"
    elif folder_id:
        path = f"/folder/{folder_id}/task"
    else:
        path = f"/team/{team_id}/task"

    try:
        all_tasks = []
        page = 0
        while True:
            params["page"] = page
            data = await _get(path, params=params)
            tasks_page = data.get("tasks", [])
            all_tasks.extend(tasks_page)
            if data.get("last_page", False) or not tasks_page:
                break
            page += 1

        tasks = [_clean_task(t) for t in all_tasks]

        # Post-filter by status type
        if status:
            sl = status.lower()
            if sl == "open":
                tasks = [t for t in tasks if t["status_type"] in ("open", "custom")]
            elif sl == "closed":
                tasks = [t for t in tasks if t["status_type"] == "done"]

        return {"status": "success", "count": len(tasks), "tasks": tasks}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

async def clickup_get_task(task_id: str) -> dict:
    """Get full details for a specific task.

    Args:
        task_id: The ClickUp task ID (e.g. 'abc123').
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured."}
    try:
        return await _get(f"/task/{task_id}")
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def clickup_create_task(
    list_id: str,
    name: str,
    description: str = "",
    due_date_ms: int | None = None,
    assignee_emails: list[str] | None = None,
    parent_task_id: str | None = None,
) -> dict:
    """Create a new task (or subtask) in a ClickUp list.

    Args:
        list_id: The list to create the task in.
        name: Task title.
        description: Task description/body.
        due_date_ms: Due date as Unix epoch milliseconds (UTC). Use clickup_timestamp helper.
        assignee_emails: List of team member emails to assign.
        parent_task_id: If provided, creates a subtask under this parent.

    Returns:
        dict with task_id and task_url on success.
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured."}

    try:
        payload: dict = {"name": name, "description": description}

        if assignee_emails:
            ids = []
            for email in assignee_emails:
                uid = await _resolve_user_id(email)
                if uid:
                    ids.append(int(uid))
            if ids:
                payload["assignees"] = ids

        if due_date_ms is not None:
            payload["due_date"] = due_date_ms
            payload["due_date_time"] = (due_date_ms % 86_400_000) != 0

        if parent_task_id:
            payload["parent"] = parent_task_id

        result = await _post(f"/list/{list_id}/task", payload)
        return {
            "status": "success",
            "task_id": result["id"],
            "task_url": result["url"],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def clickup_update_task(
    task_id: str,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    due_date_ms: int | None = None,
    add_assignee_emails: list[str] | None = None,
    remove_assignee_emails: list[str] | None = None,
) -> dict:
    """Update an existing task's fields.

    Only pass the fields you want to change — omitted fields are left untouched.

    Args:
        task_id: The task to update.
        name: New title.
        description: New description.
        status: New status (must match an existing ClickUp status name exactly).
        due_date_ms: New due date as epoch milliseconds, or 0 to clear.
        add_assignee_emails: Emails of members to add as assignees.
        remove_assignee_emails: Emails of members to remove from assignees.
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured."}

    try:
        payload: dict = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = status
        if due_date_ms is not None:
            if due_date_ms == 0:
                payload["due_date"] = None
                payload["due_date_time"] = False
            else:
                payload["due_date"] = due_date_ms
                payload["due_date_time"] = (due_date_ms % 86_400_000) != 0

        # Assignee changes
        assignees: dict = {}
        if add_assignee_emails:
            add_ids = []
            for email in add_assignee_emails:
                uid = await _resolve_user_id(email)
                if uid:
                    add_ids.append(int(uid))
            if add_ids:
                assignees["add"] = add_ids
        if remove_assignee_emails:
            rem_ids = []
            for email in remove_assignee_emails:
                uid = await _resolve_user_id(email)
                if uid:
                    rem_ids.append(int(uid))
            if rem_ids:
                assignees["rem"] = rem_ids
        if assignees:
            payload["assignees"] = assignees

        if not payload:
            return {"status": "error", "message": "No fields to update."}

        result = await _put(f"/task/{task_id}", payload)
        return {"status": "success", "task_id": result.get("id", task_id), "task_url": result.get("url", "")}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def clickup_add_comment(
    task_id: str,
    comment_text: str,
    notify_all: bool = False,
) -> dict:
    """Add a comment to a task. Use this to communicate with assignees about a task.

    Args:
        task_id: The task to comment on.
        comment_text: The comment body (plain text).
        notify_all: If True, all assignees are notified.
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured."}

    try:
        result = await _post(f"/task/{task_id}/comment", {
            "comment_text": comment_text,
            "notify_all": notify_all,
        })
        return {"status": "success", "comment_id": result.get("id", "")}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def clickup_delete_task(task_id: str) -> dict:
    """Delete a task permanently. Use with caution.

    Args:
        task_id: The task to delete.
    """
    if not _is_configured():
        return {"status": "error", "message": "CLICKUP_API_TOKEN not configured."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(f"{API_BASE}/task/{task_id}", headers=_headers())
            resp.raise_for_status()
        return {"status": "success", "message": f"Task {task_id} deleted."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clickup_timestamp(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, utc_offset_hours: int = 0,
) -> dict:
    """Convert a local date/time to a Unix timestamp in milliseconds for ClickUp.

    Args:
        year: e.g. 2026.
        month: 1-12.
        day: 1-31.
        hour: 0-23 (default 0).
        minute: 0-59 (default 0).
        utc_offset_hours: Offset from UTC in hours (e.g. +3, -5). Default 0 (UTC).

    Returns:
        dict with 'timestamp_ms' value.
    """
    local_dt = datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=utc_offset_hours)))
    utc_dt = local_dt.astimezone(timezone.utc)
    return {"status": "success", "timestamp_ms": int(utc_dt.timestamp() * 1000)}


def clickup_task_link(task_id: str) -> dict:
    """Get the direct web URL for a ClickUp task.

    Args:
        task_id: The task ID.
    """
    return {"url": f"https://app.clickup.com/t/{task_id}"}


async def _resolve_user_id(email: str) -> str | None:
    """Look up a ClickUp user ID by email from the team members list."""
    try:
        data = await _get("/team")
        for team in data.get("teams", []):
            for member in team.get("members", []):
                if member.get("user", {}).get("email") == email:
                    return str(member["user"]["id"])
    except Exception:
        pass
    return None


def _clean_task(task: dict) -> dict:
    """Extract the useful fields from a raw ClickUp task object."""
    return {
        "id": task.get("id"),
        "name": task.get("name"),
        "description": task.get("text_content") or task.get("description", ""),
        "status": task.get("status", {}).get("status", ""),
        "status_type": task.get("status", {}).get("type", ""),
        "assignees": [
            {"id": a.get("id"), "username": a.get("username"), "email": a.get("email")}
            for a in task.get("assignees", [])
        ],
        "creator": task.get("creator", {}).get("email", ""),
        "due_date": task.get("due_date"),
        "date_created": task.get("date_created"),
        "url": task.get("url"),
    }
