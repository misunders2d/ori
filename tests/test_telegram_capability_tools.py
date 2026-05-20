"""Unit tests for the capability-administration tools in
app.tools.telegram (slice 7).

`telegram_grant_capability` / `telegram_revoke_capability` perform the
mutation; the ACT+TOTP admin gate lives in `admin_tool_guardrail`
(slice 10 wires the gating). These tests cover the tool body alone.

`telegram_list_capabilities` enforces the cross-user read rule inline
(no-arg → self; explicit user_id → admin-only, proposal finding #7).
"""

from __future__ import annotations

import pytest

from app.core import capabilities
from app.tools import telegram as tg


class _StateDict(dict):
    def to_dict(self):
        return dict(self)


class _ToolContext:
    def __init__(self, user_id="tg_111"):
        self.state = _StateDict(user_id=user_id)


@pytest.fixture
def isolated_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(
        capabilities, "CAPABILITIES_PATH", str(tmp_path / "capabilities.json")
    )
    capabilities._reset_for_tests()
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    return tmp_path


# --------------------------------------------------------------------------- grant


@pytest.mark.asyncio
async def test_grant_capability_happy(isolated_caps):
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_grant_capability(
        user_id="tg_222",
        capability="send_to_groups",
        tool_context=ctx,
    )
    assert result["status"] == "success"
    assert "send_to_groups" in result["now_holds"]
    assert (
        await capabilities.has_capability("tg_222", "send_to_groups")
    ) is True


@pytest.mark.asyncio
async def test_grant_capability_unknown_name(isolated_caps):
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_grant_capability(
        user_id="tg_222",
        capability="not_a_capability",
        tool_context=ctx,
    )
    assert result["status"] == "error"
    assert "Unknown capability" in result["message"]


@pytest.mark.asyncio
async def test_grant_requires_user_id_and_capability(isolated_caps):
    ctx = _ToolContext()
    for call in (
        lambda: tg.telegram_grant_capability(
            user_id="", capability="send_to_groups", tool_context=ctx
        ),
        lambda: tg.telegram_grant_capability(
            user_id="tg_222", capability="", tool_context=ctx
        ),
    ):
        result = await call()
        assert result["status"] == "error"
        assert "required" in result["message"]


# --------------------------------------------------------------------------- revoke


@pytest.mark.asyncio
async def test_revoke_capability_happy(isolated_caps):
    await capabilities.grant("tg_222", "send_to_groups")
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_revoke_capability(
        user_id="tg_222",
        capability="send_to_groups",
        tool_context=ctx,
    )
    assert result["status"] == "success"
    assert "send_to_groups" not in result["now_holds"]
    assert (
        await capabilities.has_capability("tg_222", "send_to_groups")
    ) is False


@pytest.mark.asyncio
async def test_revoke_noop_when_missing(isolated_caps):
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_revoke_capability(
        user_id="tg_222",
        capability="send_to_groups",
        tool_context=ctx,
    )
    assert result["status"] == "success"
    assert result["now_holds"] == []


# --------------------------------------------------------------------------- list_capabilities


@pytest.mark.asyncio
async def test_list_capabilities_self_no_arg(isolated_caps):
    await capabilities.grant("tg_111", "send_to_groups")
    ctx = _ToolContext(user_id="tg_111")
    result = await tg.telegram_list_capabilities(tool_context=ctx)
    assert result["status"] == "success"
    assert result["user_id"] == "tg_111"
    assert result["capabilities"] == ["send_to_groups"]


@pytest.mark.asyncio
async def test_list_capabilities_self_via_arg_allowed(isolated_caps):
    """Passing user_id=self is the same as no-arg; no admin needed."""
    await capabilities.grant("tg_111", "manage_aliases")
    ctx = _ToolContext(user_id="tg_111")
    result = await tg.telegram_list_capabilities(
        tool_context=ctx, user_id="tg_111"
    )
    assert result["status"] == "success"
    assert result["capabilities"] == ["manage_aliases"]


@pytest.mark.asyncio
async def test_list_capabilities_cross_user_needs_admin(isolated_caps):
    """A non-admin trying to read someone else's capabilities is blocked
    inline (proposal finding #7 — no cross-user leak)."""
    await capabilities.grant("tg_222", "forward_files")
    ctx = _ToolContext(user_id="tg_111")
    result = await tg.telegram_list_capabilities(
        tool_context=ctx, user_id="tg_222"
    )
    assert result["status"] == "error"
    assert "admin-only" in result["message"]


@pytest.mark.asyncio
async def test_list_capabilities_admin_cross_user_allowed(
    isolated_caps, monkeypatch
):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    await capabilities.grant("tg_222", "forward_files")
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_list_capabilities(
        tool_context=ctx, user_id="tg_222"
    )
    assert result["status"] == "success"
    assert result["user_id"] == "tg_222"
    assert result["capabilities"] == ["forward_files"]


@pytest.mark.asyncio
async def test_list_capabilities_admin_implicit_all(
    isolated_caps, monkeypatch
):
    """Admin asking about self gets the full canonical set."""
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_list_capabilities(tool_context=ctx)
    assert result["status"] == "success"
    assert set(result["capabilities"]) == set(
        capabilities.CANONICAL_CAPABILITIES
    )


@pytest.mark.asyncio
async def test_missing_caller_user_id_refuses(isolated_caps):
    ctx = _ToolContext(user_id="")
    result = await tg.telegram_list_capabilities(tool_context=ctx)
    assert result["status"] == "error"
    assert "caller user_id" in result["message"]


# --------------------------------------------------------------------------- audit logging (slice 7 revision)


@pytest.mark.asyncio
async def test_cross_user_read_allowed_emits_info_log(
    isolated_caps, monkeypatch, caplog
):
    """Reviewer finding #2: admin cross-user reads MUST be logged at
    INFO so the audit trail covers both branches."""
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    await capabilities.grant("tg_222", "forward_files")
    ctx = _ToolContext(user_id="tg_admin")
    with caplog.at_level("INFO", logger="app.tools.telegram"):
        result = await tg.telegram_list_capabilities(
            tool_context=ctx, user_id="tg_222"
        )
    assert result["status"] == "success"
    audit = [
        rec for rec in caplog.records
        if "cross-user read ALLOWED" in rec.getMessage()
    ]
    assert len(audit) == 1
    assert "tg_admin" in audit[0].getMessage()
    assert "tg_222" in audit[0].getMessage()


@pytest.mark.asyncio
async def test_cross_user_read_denied_emits_info_log(isolated_caps, caplog):
    """Denied cross-user reads also logged at INFO for traceability."""
    ctx = _ToolContext(user_id="tg_111")
    with caplog.at_level("INFO", logger="app.tools.telegram"):
        result = await tg.telegram_list_capabilities(
            tool_context=ctx, user_id="tg_222"
        )
    assert result["status"] == "error"
    audit = [
        rec for rec in caplog.records
        if "cross-user read DENIED" in rec.getMessage()
    ]
    assert len(audit) == 1
    assert "tg_111" in audit[0].getMessage()
    assert "tg_222" in audit[0].getMessage()


# --------------------------------------------------------------------------- store/cap raising (slice 7 revision)


@pytest.mark.asyncio
async def test_grant_db_failure_returns_error_dict(
    isolated_caps, monkeypatch, caplog
):
    """If capabilities.grant raises (e.g., disk-full), the tool must
    return {status:error} per Law 6 — not propagate the exception."""

    async def boom(*a, **kw):
        raise OSError("simulated disk full")

    monkeypatch.setattr(capabilities, "grant", boom)
    ctx = _ToolContext(user_id="tg_admin")
    with caplog.at_level("ERROR", logger="app.tools.telegram"):
        result = await tg.telegram_grant_capability(
            user_id="tg_222",
            capability="send_to_groups",
            tool_context=ctx,
        )
    assert result["status"] == "error"
    assert "simulated disk full" in result["message"]
    # Stack trace logged.
    assert any(
        "capabilities.grant failed" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_revoke_db_failure_returns_error_dict(
    isolated_caps, monkeypatch
):
    async def boom(*a, **kw):
        raise OSError("simulated read-only filesystem")

    monkeypatch.setattr(capabilities, "revoke", boom)
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_revoke_capability(
        user_id="tg_222",
        capability="send_to_groups",
        tool_context=ctx,
    )
    assert result["status"] == "error"
    assert "read-only filesystem" in result["message"]


@pytest.mark.asyncio
async def test_list_capabilities_failure_returns_error_dict(
    isolated_caps, monkeypatch
):
    async def boom(user_id):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(capabilities, "list_for", boom)
    ctx = _ToolContext(user_id="tg_111")
    result = await tg.telegram_list_capabilities(tool_context=ctx)
    assert result["status"] == "error"
    assert "permission denied" in result["message"]
