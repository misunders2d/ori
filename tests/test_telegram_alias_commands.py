"""Proposal §7.7 — poller `/alias save|delete|list|resolve|help`
short-circuit tests. Drives `_handle_alias_command` directly."""

from __future__ import annotations

import pytest

from app.core import capabilities, telegram_store
from interfaces import telegram_poller


class _FakeAdapter:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


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
    return tmp_path


@pytest.fixture
def adapter():
    return _FakeAdapter()


# --------------------------------------------------------------------------- non-trigger / help


@pytest.mark.asyncio
async def test_non_alias_text_not_consumed(isolated_env, adapter):
    handled = await telegram_poller._handle_alias_command(
        adapter, "hello", chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert handled is False


@pytest.mark.asyncio
async def test_alias_bare_returns_help(isolated_env, adapter):
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias", chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_alias_help_subcommand(isolated_env, adapter):
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias help", chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


# --------------------------------------------------------------------------- save


@pytest.mark.asyncio
async def test_alias_save_without_capability(isolated_env, adapter):
    telegram_store.stash_forward(
        "tg_111", chat_id=-1001234, chat_type="supergroup", title="X"
    )
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias save engineering",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    assert "manage_aliases" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_alias_save_no_recent_forward(isolated_env, adapter):
    await capabilities.grant("tg_111", "manage_aliases")
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias save engineering",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    assert "No recent forwarded message" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_alias_save_happy(isolated_env, adapter):
    await capabilities.grant("tg_111", "manage_aliases")
    telegram_store.stash_forward(
        "tg_111", chat_id=-1001234, chat_type="supergroup",
        title="Eng Team", username="eng_chat",
    )
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias save engineering",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    assert "Saved alias" in adapter.messages[0][1]
    rows = await telegram_store.list_aliases("tg_111")
    assert len(rows) == 1
    assert rows[0]["alias"] == "engineering"
    assert rows[0]["chat_id"] == -1001234


@pytest.mark.asyncio
async def test_alias_save_without_name_returns_usage(isolated_env, adapter):
    await capabilities.grant("tg_111", "manage_aliases")
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias save", chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert handled is True
    assert "needs a name" in adapter.messages[0][1]


# --------------------------------------------------------------------------- list


@pytest.mark.asyncio
async def test_alias_list_no_capability_required(isolated_env, adapter):
    """List is self-scope; no cap required."""
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias list", chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert handled is True
    assert "No aliases" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_alias_list_populated(isolated_env, adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup", title="Eng Team"
    )
    await telegram_store.save_alias(
        "tg_111", "marketing", -1009999, "channel"
    )
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias list", chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert handled is True
    reply = adapter.messages[0][1]
    assert "engineering" in reply
    assert "marketing" in reply
    assert "tg_-1001234" in reply


# --------------------------------------------------------------------------- delete


@pytest.mark.asyncio
async def test_alias_delete_requires_capability(isolated_env, adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias delete engineering",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    assert "manage_aliases" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_alias_delete_happy(isolated_env, adapter):
    await capabilities.grant("tg_111", "manage_aliases")
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias delete engineering",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    assert "Deleted alias" in adapter.messages[0][1]
    assert await telegram_store.list_aliases("tg_111") == []


@pytest.mark.asyncio
async def test_alias_delete_miss(isolated_env, adapter):
    await capabilities.grant("tg_111", "manage_aliases")
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias delete nope",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    assert "not found" in adapter.messages[0][1]


# --------------------------------------------------------------------------- resolve


@pytest.mark.asyncio
async def test_alias_resolve_no_capability_required(isolated_env, adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup", title="Eng"
    )
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias resolve engineering",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    reply = adapter.messages[0][1]
    assert "tg_-1001234" in reply
    assert "supergroup" in reply


@pytest.mark.asyncio
async def test_alias_resolve_miss(isolated_env, adapter):
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias resolve missing",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert handled is True
    assert "not found" in adapter.messages[0][1]


# --------------------------------------------------------------------------- unknown subcommand


@pytest.mark.asyncio
async def test_alias_unknown_subcommand_shows_usage(isolated_env, adapter):
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias foobar", chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


# --------------------------------------------------------------------------- /forward command


@pytest.fixture
def adapter_with_strict(isolated_env, monkeypatch):
    """Register a fake adapter on the transport registry so
    `/forward` can reach `telegram_forward` through `get_adapter`."""
    from app.core import transport

    class _Strict:
        platform_name = "telegram"

        def __init__(self):
            self.media_calls = []
            self.copy_calls = []

        async def send_message(self, chat_id, text):
            pass

        async def send_media_strict(
            self, target_id, data, mime_type, caption="",
            *, file_id=None, file_type=None, file_ref=None,
            owner_user_id=None, file_path=None,
        ):
            self.media_calls.append(
                {"target_id": target_id, "file_id": file_id, "file_type": file_type}
            )
            return {
                "ok": True,
                "message_id": 1,
                "chat_id": target_id,
                "file_id": file_id,
                "file_type": file_type,
            }

        async def copy_message_strict(self, target_id, from_chat_id, message_id):
            self.copy_calls.append((target_id, from_chat_id, message_id))
            return {"ok": True, "message_id": 1, "chat_id": target_id}

    a = _Strict()
    orig = dict(transport._registry)
    transport.register_adapter(a)
    yield a
    transport._registry.clear()
    transport._registry.update(orig)


@pytest.mark.asyncio
async def test_forward_command_happy(adapter_with_strict, adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111", "chart", "FILE_ID", "photo"
    )
    handled = await telegram_poller._handle_forward_command(
        adapter, "/forward chart to engineering",
        chat_id=111, caller_user_id="tg_111",
    )
    assert handled is True
    assert "Forwarded" in adapter.messages[0][1]
    assert adapter_with_strict.media_calls[0]["target_id"] == -1001234
    assert adapter_with_strict.media_calls[0]["file_id"] == "FILE_ID"


@pytest.mark.asyncio
async def test_forward_command_help(adapter_with_strict, adapter):
    handled = await telegram_poller._handle_forward_command(
        adapter, "/forward help",
        chat_id=111, caller_user_id="tg_111",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_forward_command_bare_returns_usage(adapter_with_strict, adapter):
    handled = await telegram_poller._handle_forward_command(
        adapter, "/forward",
        chat_id=111, caller_user_id="tg_111",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_forward_command_bad_syntax(adapter_with_strict, adapter):
    handled = await telegram_poller._handle_forward_command(
        adapter, "/forward chart engineering",
        chat_id=111, caller_user_id="tg_111",
    )
    assert handled is True
    assert "Bad" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_forward_command_failure_surfaces_tool_error(
    adapter_with_strict, adapter
):
    """Tool returns status:error → poller prefixes with `/forward` failed."""
    handled = await telegram_poller._handle_forward_command(
        adapter, "/forward never_seen to engineering",
        chat_id=111, caller_user_id="tg_111",
    )
    assert handled is True
    assert "failed" in adapter.messages[0][1]
    assert "alias" in adapter.messages[0][1]


# --------------------------------------------------------------------------- DM-only enforcement (revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_alias_in_non_dm_silently_skipped(isolated_env, adapter, chat_type):
    """Group invocations of `/alias` are silently pass-through — the
    alias namespace is per-user and v1 keeps it DM-scoped (proposal
    §2.5)."""
    handled = await telegram_poller._handle_alias_command(
        adapter, "/alias list",
        chat_id=-1001234, chat_type=chat_type,
        caller_user_id="tg_111", session_id="tg_-1001234",
    )
    assert handled is False
    assert adapter.messages == []


# --------------------------------------------------------------------------- token-exact prefix (revision)


@pytest.mark.asyncio
async def test_aliasfoo_not_consumed(isolated_env, adapter):
    """`/aliasfoo` and `/alias_something` are NOT `/alias`."""
    for bad in ("/aliasfoo bar", "/alias_extra", "/aliasextra list"):
        adapter.messages.clear()
        handled = await telegram_poller._handle_alias_command(
            adapter, bad,
            chat_id=111, chat_type="private",
            caller_user_id="tg_111", session_id="tg_111",
        )
        assert handled is False, f"{bad!r} should NOT consume"
        assert adapter.messages == []


@pytest.mark.asyncio
async def test_forwardfoo_not_consumed(isolated_env, adapter):
    """`/forwardfoo` is NOT `/forward`."""
    for bad in ("/forwardfoo bar", "/forward_extra", "/forwardx ref to alias"):
        adapter.messages.clear()
        handled = await telegram_poller._handle_forward_command(
            adapter, bad, chat_id=111, caller_user_id="tg_111"
        )
        assert handled is False, f"{bad!r} should NOT consume"
        assert adapter.messages == []


# --------------------------------------------------------------------------- capability-store wrap (revision)


@pytest.mark.asyncio
async def test_alias_save_capability_check_db_failure(
    isolated_env, adapter, monkeypatch, caplog
):
    """If capabilities.has_capability raises in the /alias save path,
    handler must reply with an error message + log; never propagate."""

    async def boom(*a, **kw):
        raise OSError("simulated cap-store failure")

    monkeypatch.setattr(capabilities, "has_capability", boom)
    telegram_store.stash_forward(
        "tg_111", chat_id=-1001234, chat_type="supergroup", title="X"
    )
    with caplog.at_level("ERROR", logger="interfaces.telegram_poller"):
        handled = await telegram_poller._handle_alias_command(
            adapter, "/alias save engineering",
            chat_id=111, chat_type="private",
            caller_user_id="tg_111", session_id="tg_111",
        )
    assert handled is True
    assert "capability check failed" in adapter.messages[0][1]
    assert any(
        "/alias save capability check failed" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_alias_delete_capability_check_db_failure(
    isolated_env, adapter, monkeypatch, caplog
):
    async def boom(*a, **kw):
        raise OSError("simulated cap-store failure")

    monkeypatch.setattr(capabilities, "has_capability", boom)
    with caplog.at_level("ERROR", logger="interfaces.telegram_poller"):
        handled = await telegram_poller._handle_alias_command(
            adapter, "/alias delete engineering",
            chat_id=111, chat_type="private",
            caller_user_id="tg_111", session_id="tg_111",
        )
    assert handled is True
    assert "capability check failed" in adapter.messages[0][1]
    assert any(
        "/alias delete capability check failed" in rec.getMessage()
        for rec in caplog.records
    )


# --------------------------------------------------------------------------- _run_short_circuits integration (revision)


@pytest.mark.asyncio
async def test_run_short_circuits_captures_forward_only_message(
    isolated_env, adapter
):
    """REGRESSION (reviewer #1): a forwarded message with NO text and
    NO file must be captured by forward-extract via the orchestrator,
    not silently dropped. This pins the wiring layer that the poll
    loop's `if await _run_short_circuits(...): continue` relies on."""
    msg = {
        # Note: no text, no caption, no file_id, no file_info_text-
        # producing payload — only forward_origin.
        "forward_origin": {
            "type": "channel",
            "chat": {
                "id": -1001234567890,
                "title": "Eng Team",
                "username": "eng_chat",
                "type": "channel",
            },
        }
    }
    consumed = await telegram_poller._run_short_circuits(
        adapter=adapter,
        msg=msg,
        text="",
        chat_id=111,
        chat_type="private",
        caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert consumed is True
    # Stashed for /alias save.
    cap = telegram_store.peek_forward("tg_111")
    assert cap is not None
    assert cap.chat_id == -1001234567890


@pytest.mark.asyncio
async def test_run_short_circuits_passthrough_for_normal_text(
    isolated_env, adapter
):
    consumed = await telegram_poller._run_short_circuits(
        adapter=adapter, msg={"text": "hello"}, text="hello",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
        session_id="tg_111",
    )
    assert consumed is False
    assert adapter.messages == []


@pytest.mark.asyncio
async def test_run_short_circuits_alias_dm_only(isolated_env, adapter):
    """Inside _run_short_circuits, `/alias` in a group returns False
    (silent DM-only enforcement)."""
    consumed = await telegram_poller._run_short_circuits(
        adapter=adapter, msg={"text": "/alias list"}, text="/alias list",
        chat_id=-1001234, chat_type="supergroup",
        caller_user_id="tg_111", session_id="tg_-1001234",
    )
    assert consumed is False
    assert adapter.messages == []


@pytest.mark.asyncio
async def test_run_short_circuits_runs_in_documented_order(
    isolated_env, adapter
):
    """Forward-extract MUST win over /savefile when a forwarded message
    happens to have an attachment + /savefile caption. (Edge case: a
    user forwards a channel post that itself has a /savefile-shaped
    caption; we still want forward-extract semantics.)"""
    msg = {
        "forward_origin": {
            "type": "channel",
            "chat": {"id": -1001234, "title": "X", "type": "channel"},
        },
        "document": {"file_id": "DOC_ID", "file_name": "x.pdf"},
    }
    consumed = await telegram_poller._run_short_circuits(
        adapter=adapter, msg=msg, text="/savefile mydoc",
        chat_id=111, chat_type="private",
        caller_user_id="tg_111", session_id="tg_111",
    )
    assert consumed is True
    # forward_extract reply, not savefile reply.
    assert "Forwarded from" in adapter.messages[0][1]
    # No cache row (savefile didn't run).
    assert await telegram_store.list_files("tg_111") == []
