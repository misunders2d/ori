"""Tests for ClickUp tools — mocked HTTP, no API key needed."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.tools.clickup import (
    clickup_get_workspace,
    clickup_list_folders_and_lists,
    clickup_list_tasks,
    clickup_get_task,
    clickup_create_task,
    clickup_update_task,
    clickup_add_comment,
    clickup_delete_task,
    clickup_timestamp,
    clickup_task_link,
    _clean_task,
    _resolve_user_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_context(user_id="test@example.com"):
    ctx = MagicMock()
    ctx.state.to_dict.return_value = {"user_id": user_id}
    ctx.state.__setitem__ = MagicMock()
    return ctx


_TEAM_RESPONSE = {
    "teams": [{
        "id": "team1",
        "name": "TestTeam",
        "members": [
            {"user": {"id": 123, "username": "alice", "email": "alice@example.com"}},
            {"user": {"id": 456, "username": "bob", "email": "bob@example.com"}},
        ],
    }]
}

_SPACE_RESPONSE = {
    "spaces": [{
        "id": "space1",
        "name": "TestSpace",
        "statuses": [{"status": "open"}, {"status": "done"}],
    }]
}


# ---------------------------------------------------------------------------
# Sync / pure function tests
# ---------------------------------------------------------------------------

def test_clickup_timestamp():
    result = clickup_timestamp(2026, 4, 7, 10, 30, utc_offset_hours=3)
    assert result["status"] == "success"
    assert isinstance(result["timestamp_ms"], int)
    assert result["timestamp_ms"] > 0


def test_clickup_timestamp_utc():
    result = clickup_timestamp(2026, 1, 1)
    assert result["status"] == "success"
    # 2026-01-01 00:00 UTC in ms
    assert result["timestamp_ms"] == 1767225600000


def test_clickup_task_link():
    result = clickup_task_link("abc123")
    assert result["url"] == "https://app.clickup.com/t/abc123"


def test_clean_task():
    raw = {
        "id": "t1",
        "name": "Test Task",
        "text_content": "Some text",
        "description": "Fallback desc",
        "status": {"status": "open", "type": "open"},
        "assignees": [{"id": 1, "username": "alice", "email": "alice@example.com"}],
        "creator": {"email": "bob@example.com"},
        "due_date": "1700000000000",
        "date_created": "1699000000000",
        "url": "https://app.clickup.com/t/t1",
    }
    clean = _clean_task(raw)
    assert clean["id"] == "t1"
    assert clean["description"] == "Some text"  # prefers text_content
    assert clean["status"] == "open"
    assert clean["creator"] == "bob@example.com"
    assert len(clean["assignees"]) == 1


def test_clean_task_fallback_description():
    raw = {
        "id": "t2",
        "name": "No text_content",
        "text_content": None,
        "description": "Fallback",
        "status": {"status": "todo", "type": "custom"},
        "assignees": [],
        "creator": {},
        "due_date": None,
        "date_created": None,
        "url": "",
    }
    clean = _clean_task(raw)
    assert clean["description"] == "Fallback"


# ---------------------------------------------------------------------------
# Async tests — mocked HTTP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_workspace_not_configured():
    with patch("app.tools.clickup._is_configured", return_value=False):
        result = await clickup_get_workspace(_make_tool_context())
        assert result["status"] == "error"
        assert "not configured" in result["message"]


@pytest.mark.asyncio
async def test_get_workspace_success():
    ctx = _make_tool_context("alice@example.com")
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("app.tools.clickup._get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [_TEAM_RESPONSE, _SPACE_RESPONSE]
        result = await clickup_get_workspace(ctx)
        assert result["user_email"] == "alice@example.com"
        assert len(result["teams"]) == 1
        assert len(result["teams"][0]["members"]) == 2
        assert len(result["spaces"]) == 1


@pytest.mark.asyncio
async def test_list_folders_and_lists_not_configured():
    with patch("app.tools.clickup._is_configured", return_value=False):
        result = await clickup_list_folders_and_lists("space1")
        assert result["status"] == "error"


@pytest.mark.asyncio
async def test_list_folders_and_lists_success():
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("app.tools.clickup._get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [
            {"folders": [{"id": "f1", "name": "Folder1"}]},
            {"lists": [{"id": "l1", "name": "List1"}]},
        ]
        result = await clickup_list_folders_and_lists("space1")
        assert len(result["folders"]) == 1
        assert len(result["lists"]) == 1


@pytest.mark.asyncio
async def test_list_tasks_no_email():
    ctx = _make_tool_context("")
    with patch("app.tools.clickup._is_configured", return_value=True):
        result = await clickup_list_tasks("team1", ctx)
        assert result["status"] == "error"
        assert "email" in result["message"].lower()


@pytest.mark.asyncio
async def test_list_tasks_success():
    ctx = _make_tool_context("alice@example.com")
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("app.tools.clickup._resolve_user_id", new_callable=AsyncMock, return_value="123"), \
         patch("app.tools.clickup._get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {
            "tasks": [
                {"id": "t1", "name": "Task 1", "text_content": "", "description": "",
                 "status": {"status": "open", "type": "open"}, "assignees": [],
                 "creator": {}, "due_date": None, "date_created": None, "url": ""},
            ],
            "last_page": True,
        }
        result = await clickup_list_tasks("team1", ctx, status="open")
        assert result["status"] == "success"
        assert result["count"] == 1


@pytest.mark.asyncio
async def test_get_task_success():
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("app.tools.clickup._get", new_callable=AsyncMock, return_value={"id": "t1", "name": "Task"}):
        result = await clickup_get_task("t1")
        assert result["id"] == "t1"


@pytest.mark.asyncio
async def test_create_task_success():
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("app.tools.clickup._resolve_user_id", new_callable=AsyncMock, return_value="123"), \
         patch("app.tools.clickup._post", new_callable=AsyncMock, return_value={"id": "new1", "url": "https://app.clickup.com/t/new1"}):
        result = await clickup_create_task(
            list_id="l1",
            name="New Task",
            description="Test",
            assignee_emails=["alice@example.com"],
        )
        assert result["status"] == "success"
        assert result["task_id"] == "new1"


@pytest.mark.asyncio
async def test_update_task_success():
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("app.tools.clickup._put", new_callable=AsyncMock, return_value={"id": "t1", "url": "https://app.clickup.com/t/t1"}):
        result = await clickup_update_task("t1", status="done")
        assert result["status"] == "success"


@pytest.mark.asyncio
async def test_update_task_no_fields():
    with patch("app.tools.clickup._is_configured", return_value=True):
        result = await clickup_update_task("t1")
        assert result["status"] == "error"
        assert "No fields" in result["message"]


@pytest.mark.asyncio
async def test_add_comment_success():
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("app.tools.clickup._post", new_callable=AsyncMock, return_value={"id": "c1"}):
        result = await clickup_add_comment("t1", "Hey, what's the status?", notify_all=True)
        assert result["status"] == "success"


@pytest.mark.asyncio
async def test_delete_task_success():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    with patch("app.tools.clickup._is_configured", return_value=True), \
         patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_class.return_value = mock_client
        result = await clickup_delete_task("t1")
        assert result["status"] == "success"


@pytest.mark.asyncio
async def test_resolve_user_id():
    with patch("app.tools.clickup._get", new_callable=AsyncMock, return_value=_TEAM_RESPONSE):
        uid = await _resolve_user_id("alice@example.com")
        assert uid == "123"


@pytest.mark.asyncio
async def test_resolve_user_id_not_found():
    with patch("app.tools.clickup._get", new_callable=AsyncMock, return_value=_TEAM_RESPONSE):
        uid = await _resolve_user_id("nobody@example.com")
        assert uid is None
