"""Unit tests for the alias-management tools in app.tools.telegram
(slice 7 — proposal §2.8 cap matrix).

Capability matrix (locked in v3):
    - save / delete / save_last_forward → manage_aliases
    - list / resolve → none (self-scope)
"""

from __future__ import annotations

import pytest

from app.core import capabilities, telegram_store, transport
from app.tools import telegram as tg


class _StateDict(dict):
    def to_dict(self):
        return dict(self)


class _ToolContext:
    def __init__(self, user_id="tg_111", session_id="tg_chat_111"):
        self.state = _StateDict(user_id=user_id, session_id=session_id)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        capabilities, "CAPABILITIES_PATH", str(tmp_path / "capabilities.json")
    )
    capabilities._reset_for_tests()
    monkeypatch.setattr(
        telegram_store, "DB_PATH", str(tmp_path / "telegram_skills.db")
    )
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    telegram_store._reset_for_tests()
    yield tmp_path


# --------------------------------------------------------------------------- save


@pytest.mark.asyncio
async def test_save_alias_requires_manage_aliases(isolated_env):
    ctx = _ToolContext()
    result = await tg.telegram_save_alias(
        alias="engineering",
        chat_id=-1001234,
        chat_type="supergroup",
        tool_context=ctx,
    )
    assert result["status"] == "error"
    assert "manage_aliases" in result["message"]


@pytest.mark.asyncio
async def test_save_alias_happy(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    result = await tg.telegram_save_alias(
        alias="engineering",
        chat_id=-1001234,
        chat_type="supergroup",
        title="Eng Team",
        username="eng_chat",
        tool_context=ctx,
    )
    assert result["status"] == "success"
    assert result["alias"] == "engineering"
    rows = await telegram_store.list_aliases("tg_111")
    assert len(rows) == 1
    assert rows[0]["title"] == "Eng Team"


@pytest.mark.asyncio
async def test_save_alias_rejects_dm_type(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    result = await tg.telegram_save_alias(
        alias="myself",
        chat_id=111,
        chat_type="user",  # DM aliases NOT supported v1
        tool_context=ctx,
    )
    assert result["status"] == "error"
    assert "chat_type" in result["message"]


@pytest.mark.asyncio
async def test_save_alias_chat_id_must_be_int(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    result = await tg.telegram_save_alias(
        alias="engineering",
        chat_id="not-an-int",
        chat_type="supergroup",
        tool_context=ctx,
    )
    assert result["status"] == "error"
    assert "chat_id" in result["message"]


# --------------------------------------------------------------------------- list / resolve (no cap)


@pytest.mark.asyncio
async def test_list_aliases_no_capability_required(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    await tg.telegram_save_alias(
        alias="a", chat_id=-1, chat_type="supergroup", tool_context=ctx
    )
    await tg.telegram_save_alias(
        alias="b", chat_id=-2, chat_type="channel", tool_context=ctx
    )

    # Strip the cap and confirm list still works (self-scope).
    await capabilities.revoke("tg_111", "manage_aliases")
    result = await tg.telegram_list_aliases(tool_context=ctx)
    assert result["status"] == "success"
    aliases = sorted(a["alias"] for a in result["aliases"])
    assert aliases == ["a", "b"]


@pytest.mark.asyncio
async def test_resolve_alias_no_capability_required(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    await tg.telegram_save_alias(
        alias="engineering",
        chat_id=-1001234,
        chat_type="supergroup",
        tool_context=ctx,
    )
    await capabilities.revoke("tg_111", "manage_aliases")
    result = await tg.telegram_resolve_alias(
        alias="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    assert result["chat_id"] == -1001234


@pytest.mark.asyncio
async def test_resolve_alias_miss(isolated_env):
    ctx = _ToolContext()
    result = await tg.telegram_resolve_alias(
        alias="nope", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "not found" in result["message"]


@pytest.mark.asyncio
async def test_list_owner_scoping(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    await capabilities.grant("tg_222", "manage_aliases")
    ctx_a = _ToolContext(user_id="tg_111")
    ctx_b = _ToolContext(user_id="tg_222")
    await tg.telegram_save_alias(
        alias="engineering", chat_id=-1, chat_type="supergroup",
        tool_context=ctx_a,
    )
    await tg.telegram_save_alias(
        alias="engineering", chat_id=-9, chat_type="channel",
        tool_context=ctx_b,
    )
    a_result = await tg.telegram_list_aliases(tool_context=ctx_a)
    b_result = await tg.telegram_list_aliases(tool_context=ctx_b)
    assert len(a_result["aliases"]) == 1
    assert a_result["aliases"][0]["chat_id"] == -1
    assert len(b_result["aliases"]) == 1
    assert b_result["aliases"][0]["chat_id"] == -9


# --------------------------------------------------------------------------- delete


@pytest.mark.asyncio
async def test_delete_alias_requires_manage_aliases(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    await tg.telegram_save_alias(
        alias="engineering", chat_id=-1, chat_type="supergroup",
        tool_context=ctx,
    )
    await capabilities.revoke("tg_111", "manage_aliases")
    result = await tg.telegram_delete_alias(
        alias="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "manage_aliases" in result["message"]


@pytest.mark.asyncio
async def test_delete_alias_happy(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    await tg.telegram_save_alias(
        alias="engineering", chat_id=-1, chat_type="supergroup",
        tool_context=ctx,
    )
    result = await tg.telegram_delete_alias(
        alias="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    assert await telegram_store.list_aliases("tg_111") == []


@pytest.mark.asyncio
async def test_delete_miss_returns_error(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    result = await tg.telegram_delete_alias(
        alias="never_existed", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "not found" in result["message"]


# --------------------------------------------------------------------------- save_last_forward


@pytest.mark.asyncio
async def test_save_last_forward_alias_happy(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    telegram_store.stash_forward(
        "tg_chat_111",
        chat_id=-1001234,
        chat_type="supergroup",
        title="Eng Team",
        username="eng_chat",
    )
    ctx = _ToolContext()
    result = await tg.telegram_save_last_forward_alias(
        alias="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    assert result["chat_id"] == -1001234

    # The stashed entry was consumed (pop semantics).
    assert telegram_store.peek_forward("tg_chat_111") is None


@pytest.mark.asyncio
async def test_save_last_forward_alias_no_stash(isolated_env):
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext()
    result = await tg.telegram_save_last_forward_alias(
        alias="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "No recent forwarded message" in result["message"]


@pytest.mark.asyncio
async def test_save_last_forward_requires_capability(isolated_env):
    telegram_store.stash_forward(
        "tg_chat_111", chat_id=-1, chat_type="supergroup"
    )
    ctx = _ToolContext()
    result = await tg.telegram_save_last_forward_alias(
        alias="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "manage_aliases" in result["message"]


# --------------------------------------------------------------------------- missing context


@pytest.mark.asyncio
async def test_missing_caller_user_id_refuses(isolated_env):
    ctx = _ToolContext(user_id="")
    for call in (
        lambda: tg.telegram_save_alias(
            alias="x", chat_id=-1, chat_type="supergroup", tool_context=ctx
        ),
        lambda: tg.telegram_list_aliases(tool_context=ctx),
        lambda: tg.telegram_delete_alias(alias="x", tool_context=ctx),
        lambda: tg.telegram_resolve_alias(alias="x", tool_context=ctx),
        lambda: tg.telegram_save_last_forward_alias(
            alias="x", tool_context=ctx
        ),
    ):
        result = await call()
        assert result["status"] == "error"
        assert "caller user_id" in result["message"]


# --------------------------------------------------------------------------- store raises (slice 7 revision)


@pytest.mark.asyncio
async def test_save_alias_db_failure_returns_error_dict(
    isolated_env, monkeypatch, caplog
):
    """If the store layer raises a non-ValueError exception, the tool
    must return {status:error} per Law 6 — not propagate."""
    await capabilities.grant("tg_111", "manage_aliases")

    async def boom(**kw):
        raise OSError("simulated database is locked")

    monkeypatch.setattr(telegram_store, "save_alias", boom)
    ctx = _ToolContext()
    with caplog.at_level("ERROR", logger="app.tools.telegram"):
        result = await tg.telegram_save_alias(
            alias="x", chat_id=-1, chat_type="supergroup", tool_context=ctx
        )
    assert result["status"] == "error"
    assert "database is locked" in result["message"]
    assert any(
        "telegram_store.save_alias failed" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_list_aliases_db_failure_returns_error_dict(
    isolated_env, monkeypatch
):
    async def boom(*a, **kw):
        raise OSError("simulated read failure")

    monkeypatch.setattr(telegram_store, "list_aliases", boom)
    ctx = _ToolContext()
    result = await tg.telegram_list_aliases(tool_context=ctx)
    assert result["status"] == "error"
    assert "read failure" in result["message"]


@pytest.mark.asyncio
async def test_delete_alias_db_failure_returns_error_dict(
    isolated_env, monkeypatch
):
    await capabilities.grant("tg_111", "manage_aliases")

    async def boom(*a, **kw):
        raise OSError("simulated delete failure")

    monkeypatch.setattr(telegram_store, "delete_alias", boom)
    ctx = _ToolContext()
    result = await tg.telegram_delete_alias(alias="x", tool_context=ctx)
    assert result["status"] == "error"
    assert "delete failure" in result["message"]


@pytest.mark.asyncio
async def test_resolve_alias_db_failure_returns_error_dict(
    isolated_env, monkeypatch
):
    async def boom(*a, **kw):
        raise OSError("simulated read failure")

    monkeypatch.setattr(telegram_store, "resolve_alias", boom)
    ctx = _ToolContext()
    result = await tg.telegram_resolve_alias(alias="x", tool_context=ctx)
    assert result["status"] == "error"
    assert "read failure" in result["message"]


@pytest.mark.asyncio
async def test_capability_check_failure_returns_error_dict(
    isolated_env, monkeypatch, caplog
):
    """If capabilities.has_capability raises, the cap-gated tool must
    return {status:error}, not propagate the exception."""

    async def boom(*a, **kw):
        raise OSError("simulated cap-store failure")

    monkeypatch.setattr(capabilities, "has_capability", boom)
    ctx = _ToolContext()
    with caplog.at_level("ERROR", logger="app.tools.telegram"):
        result = await tg.telegram_save_alias(
            alias="x", chat_id=-1, chat_type="supergroup", tool_context=ctx
        )
    assert result["status"] == "error"
    assert "capability check failed" in result["message"]
    assert any(
        "capabilities.has_capability raised" in rec.getMessage()
        for rec in caplog.records
    )
