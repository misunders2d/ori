"""Unit tests for app.tools.telegram.telegram_send_to_chat (proposal §7.4).

Covers:
    - happy: alias resolves → fake strict adapter records (chat_id, text).
    - caller lacks `send_to_groups` → error, no adapter call.
    - bot-not-member: fake adapter returns 403 + description; tool
      returns status:error with exact description.
    - raw `chat_id="-1001234"` works without an alias.
    - DM target (positive id) → no capability required.
"""

from __future__ import annotations

import pytest

from app.core import capabilities, telegram_store, transport
from app.tools import telegram as tg


class _FakeStrictAdapter:
    platform_name = "telegram"

    def __init__(self):
        self.calls: list[dict] = []
        self.response: dict | None = None

    async def send_text_strict(self, target_id, text):
        self.calls.append({"target_id": target_id, "text": text})
        if self.response is not None:
            return self.response
        return {
            "ok": True,
            "message_id": 1,
            "chat_id": target_id,
        }


class _StateDict(dict):
    """state object that mimics ADK ToolContext.state — supports .get()."""

    def to_dict(self):
        return dict(self)


class _ToolContext:
    def __init__(self, user_id="tg_111", session_id="tg_chat_111"):
        self.state = _StateDict(user_id=user_id, session_id=session_id)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """All slice 7 tools touch capabilities + telegram_store + transport
    registry; isolate every one of them per test."""
    monkeypatch.setattr(
        capabilities, "CAPABILITIES_PATH", str(tmp_path / "capabilities.json")
    )
    capabilities._reset_for_tests()
    monkeypatch.setattr(
        telegram_store, "DB_PATH", str(tmp_path / "telegram_skills.db")
    )
    monkeypatch.delenv("TELEGRAM_FILE_CACHE_TTL_HOURS", raising=False)
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    telegram_store._reset_for_tests()
    original_registry = dict(transport._registry)
    yield tmp_path
    transport._registry.clear()
    transport._registry.update(original_registry)


@pytest.fixture
def adapter(isolated_env):
    a = _FakeStrictAdapter()
    transport.register_adapter(a)
    return a


@pytest.mark.asyncio
async def test_happy_path_alias_resolves_and_capability_passes(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup", title="Eng Team"
    )
    await capabilities.grant("tg_111", "send_to_groups")

    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="engineering",
        text="deploy rolling out",
        tool_context=ctx,
    )
    assert result["status"] == "success"
    assert result["chat_id"] == -1001234
    assert result["alias"] == "engineering"
    assert adapter.calls == [
        {"target_id": -1001234, "text": "deploy rolling out"}
    ]


@pytest.mark.asyncio
async def test_caller_lacks_capability(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="engineering",
        text="hi",
        tool_context=ctx,
    )
    assert result["status"] == "error"
    assert "send_to_groups" in result["message"]
    # No adapter call because cap-check fails first.
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_bot_not_member_propagates_description_verbatim(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "send_to_groups")
    adapter.response = {
        "ok": False,
        "error_code": 403,
        "description": "Forbidden: bot is not a member of the supergroup chat",
    }
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="engineering", text="hi", tool_context=ctx
    )
    assert result["status"] == "error"
    assert result["error_code"] == 403
    assert (
        result["message"]
        == "Forbidden: bot is not a member of the supergroup chat"
    )


@pytest.mark.asyncio
async def test_raw_negative_chat_id_works_with_capability(adapter):
    await capabilities.grant("tg_111", "send_to_groups")
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="-1001234567", text="hi", tool_context=ctx
    )
    assert result["status"] == "success"
    assert adapter.calls[0]["target_id"] == -1001234567


@pytest.mark.asyncio
async def test_raw_negative_chat_id_without_capability_blocked(adapter):
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="-1001234567", text="hi", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "send_to_groups" in result["message"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_dm_shaped_positive_chat_id_skips_capability(adapter):
    """Positive raw chat_id is DM-shaped per proposal §12 v2-c. No cap
    required; Telegram fails loud if it isn't actually a DM."""
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="555", text="hi", tool_context=ctx
    )
    assert result["status"] == "success"
    assert adapter.calls[0]["target_id"] == 555


@pytest.mark.asyncio
async def test_tg_prefixed_raw_target(adapter):
    await capabilities.grant("tg_111", "send_to_groups")
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="tg_-1001234567", text="hi", tool_context=ctx
    )
    assert result["status"] == "success"
    assert adapter.calls[0]["target_id"] == -1001234567


@pytest.mark.asyncio
async def test_unknown_alias_returns_error(adapter):
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="does_not_exist", text="hi", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "alias 'does_not_exist' not found" in result["message"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_missing_caller_user_id_refuses(adapter):
    ctx = _ToolContext(user_id="")  # state.user_id is empty
    result = await tg.telegram_send_to_chat(
        target="-1001234567", text="hi", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "caller user_id" in result["message"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_admin_implicit_capability_bypass(adapter, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_send_to_chat(
        target="-1001234567", text="hi", tool_context=ctx
    )
    assert result["status"] == "success"


# --------------------------------------------------------------------------- store raises (slice 7 revision)


@pytest.mark.asyncio
async def test_alias_lookup_db_failure_returns_error_dict(
    adapter, monkeypatch, caplog
):
    """resolve_alias raising (e.g., DB locked) must be surfaced as
    {status:error}, not propagate."""

    async def boom(*a, **kw):
        raise OSError("simulated DB lock")

    monkeypatch.setattr(telegram_store, "resolve_alias", boom)
    ctx = _ToolContext()
    with caplog.at_level("ERROR", logger="app.tools.telegram"):
        result = await tg.telegram_send_to_chat(
            target="engineering", text="hi", tool_context=ctx
        )
    assert result["status"] == "error"
    assert "alias lookup failed" in result["message"]
    assert any(
        "telegram_store.resolve_alias failed" in rec.getMessage()
        for rec in caplog.records
    )
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_capability_check_db_failure_returns_error_dict(
    adapter, monkeypatch
):
    """capabilities.has_capability raising must surface as
    {status:error} — not block the call from short-circuiting cleanly."""

    async def boom(*a, **kw):
        raise OSError("simulated cap-store failure")

    monkeypatch.setattr(capabilities, "has_capability", boom)
    ctx = _ToolContext()
    result = await tg.telegram_send_to_chat(
        target="-1001234567", text="hi", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "capability check failed" in result["message"]
    assert adapter.calls == []
