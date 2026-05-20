"""Proposal §7.8 — poller `/cap grant|revoke|list` short-circuit tests.

Drives `_handle_cap_command` directly. /cap is admin-only (deterministic,
no ACT+TOTP — parity with /models set, reviewer A6)."""

from __future__ import annotations

import pytest

from app.core import capabilities
from interfaces import telegram_poller


class _FakeAdapter:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


@pytest.fixture
def isolated_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(
        capabilities, "CAPABILITIES_PATH", str(tmp_path / "capabilities.json")
    )
    capabilities._reset_for_tests()
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    return tmp_path


@pytest.fixture
def adapter():
    return _FakeAdapter()


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")


# --------------------------------------------------------------------------- non-trigger


@pytest.mark.asyncio
async def test_non_cap_text_not_consumed(isolated_caps, adapter):
    handled = await telegram_poller._handle_cap_command(
        adapter, "hello", chat_id=111, caller_user_id="tg_admin"
    )
    assert handled is False


@pytest.mark.asyncio
async def test_cap_bare_returns_help(isolated_caps, adapter):
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap", chat_id=111, caller_user_id="tg_111"
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


# --------------------------------------------------------------------------- list


@pytest.mark.asyncio
async def test_cap_list_self_no_arg(isolated_caps, adapter):
    await capabilities.grant("tg_111", "send_to_groups")
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap list", chat_id=111, caller_user_id="tg_111"
    )
    assert handled is True
    assert "send_to_groups" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_cap_list_self_via_arg_allowed(isolated_caps, adapter):
    """Non-admin can list themselves by name (target == caller)."""
    await capabilities.grant("tg_111", "manage_aliases")
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap list tg_111", chat_id=111, caller_user_id="tg_111"
    )
    assert handled is True
    assert "manage_aliases" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_cap_list_cross_user_needs_admin(isolated_caps, adapter, caplog):
    await capabilities.grant("tg_222", "send_to_groups")
    with caplog.at_level("INFO", logger="interfaces.telegram_poller"):
        handled = await telegram_poller._handle_cap_command(
            adapter, "/cap list tg_222",
            chat_id=111, caller_user_id="tg_111",
        )
    assert handled is True
    assert "cannot list" in adapter.messages[0][1]
    # Denied audit log present.
    assert any(
        "cross-user read DENIED" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_cap_list_cross_user_admin_allowed(
    isolated_caps, adapter, admin_env, caplog
):
    await capabilities.grant("tg_222", "forward_files")
    with caplog.at_level("INFO", logger="interfaces.telegram_poller"):
        handled = await telegram_poller._handle_cap_command(
            adapter, "/cap list tg_222",
            chat_id=111, caller_user_id="tg_admin",
        )
    assert handled is True
    assert "forward_files" in adapter.messages[0][1]
    # Allowed audit log present.
    assert any(
        "cross-user read ALLOWED" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_cap_list_empty(isolated_caps, adapter):
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap list", chat_id=111, caller_user_id="tg_111"
    )
    assert handled is True
    assert "no capabilities" in adapter.messages[0][1]


# --------------------------------------------------------------------------- grant / revoke (admin-only)


@pytest.mark.asyncio
async def test_cap_grant_non_admin_blocked(isolated_caps, adapter):
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap grant tg_222 send_to_groups",
        chat_id=111, caller_user_id="tg_111",
    )
    assert handled is True
    assert "admin-only" in adapter.messages[0][1]
    assert (
        await capabilities.has_capability("tg_222", "send_to_groups")
    ) is False


@pytest.mark.asyncio
async def test_cap_grant_admin_happy(
    isolated_caps, adapter, admin_env, caplog
):
    with caplog.at_level("INFO", logger="interfaces.telegram_poller"):
        handled = await telegram_poller._handle_cap_command(
            adapter, "/cap grant tg_222 send_to_groups",
            chat_id=111, caller_user_id="tg_admin",
        )
    assert handled is True
    assert "Granted" in adapter.messages[0][1]
    assert (
        await capabilities.has_capability("tg_222", "send_to_groups")
    ) is True
    # Mutation audit log.
    assert any(
        "/cap grant by admin" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_cap_revoke_admin_happy(isolated_caps, adapter, admin_env):
    await capabilities.grant("tg_222", "send_to_groups")
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap revoke tg_222 send_to_groups",
        chat_id=111, caller_user_id="tg_admin",
    )
    assert handled is True
    assert "Revoked" in adapter.messages[0][1]
    assert (
        await capabilities.has_capability("tg_222", "send_to_groups")
    ) is False


@pytest.mark.asyncio
async def test_cap_grant_unknown_capability(isolated_caps, adapter, admin_env):
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap grant tg_222 made_up_cap",
        chat_id=111, caller_user_id="tg_admin",
    )
    assert handled is True
    assert "rejected" in adapter.messages[0][1]
    assert "Unknown capability" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_cap_grant_missing_args(isolated_caps, adapter, admin_env):
    handled = await telegram_poller._handle_cap_command(
        adapter, "/cap grant tg_222",
        chat_id=111, caller_user_id="tg_admin",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


# --------------------------------------------------------------------------- token-exact prefix (revision)


@pytest.mark.asyncio
async def test_capfoo_not_consumed(isolated_caps, adapter):
    """`/capfoo` and `/cap_extra` are NOT `/cap`."""
    for bad in ("/capfoo", "/cap_extra grant", "/capx list"):
        adapter.messages.clear()
        handled = await telegram_poller._handle_cap_command(
            adapter, bad, chat_id=111, caller_user_id="tg_admin",
        )
        assert handled is False, f"{bad!r} should NOT consume"
        assert adapter.messages == []
