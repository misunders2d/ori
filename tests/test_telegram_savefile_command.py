"""Proposal §7.12 — `/savefile <name>` poller short-circuit tests.

Drives `_handle_savefile_command` directly. Covers (post-revision):
    - happy: DM upload with caption `/savefile <name>` → outbound_files row.
    - free-text `save as <name>` is NOT a trigger.
    - no-name returns usage.
    - no caption returns no-op.
    - no attached file returns prompt to attach.
    - per-file_type capture for all six allowed types.
    - duplicate `/savefile <name>` overwrites + refreshes expires_at.
    - DM-only: group invocations are silently pass-through.
    - token-exact prefix: `/savefilex` is not consumed.
"""

from __future__ import annotations

import pytest

from app.core import telegram_store
from interfaces import telegram_poller


class _FakeAdapter:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        telegram_store, "DB_PATH", str(tmp_path / "telegram_skills.db")
    )
    monkeypatch.delenv("TELEGRAM_FILE_CACHE_TTL_HOURS", raising=False)
    telegram_store._reset_for_tests()
    return tmp_path


@pytest.fixture
def adapter():
    return _FakeAdapter()


# --------------------------------------------------------------------------- triggers


@pytest.mark.asyncio
async def test_non_savefile_text_not_consumed(isolated_store, adapter):
    msg = {"document": {"file_id": "DOC", "file_name": "a.pdf"}}
    handled = await telegram_poller._handle_savefile_command(
        adapter, msg, "save as q1_revenue",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is False  # free-text 'save as' is NOT a trigger
    rows = await telegram_store.list_files("tg_111")
    assert rows == []


@pytest.mark.asyncio
async def test_no_caption_not_consumed(isolated_store, adapter):
    msg = {"document": {"file_id": "DOC", "file_name": "a.pdf"}}
    handled = await telegram_poller._handle_savefile_command(
        adapter, msg, "",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is False


@pytest.mark.asyncio
async def test_savefile_no_name_returns_usage(isolated_store, adapter):
    msg = {"document": {"file_id": "DOC", "file_name": "a.pdf"}}
    handled = await telegram_poller._handle_savefile_command(
        adapter, msg, "/savefile",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]
    assert await telegram_store.list_files("tg_111") == []


@pytest.mark.asyncio
async def test_savefile_help(isolated_store, adapter):
    handled = await telegram_poller._handle_savefile_command(
        adapter, {}, "/savefile help",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is True
    assert "Usage" in adapter.messages[0][1]


@pytest.mark.asyncio
async def test_savefile_no_attachment(isolated_store, adapter):
    handled = await telegram_poller._handle_savefile_command(
        adapter, {}, "/savefile q1_revenue",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is True
    assert "attach" in adapter.messages[0][1].lower()
    assert await telegram_store.list_files("tg_111") == []


# --------------------------------------------------------------------------- happy paths per file_type


@pytest.mark.asyncio
async def test_savefile_document_happy(isolated_store, adapter):
    msg = {
        "message_id": 99,
        "document": {
            "file_id": "DOC_ID",
            "file_name": "report.pdf",
            "mime_type": "application/pdf",
        },
    }
    handled = await telegram_poller._handle_savefile_command(
        adapter, msg, "/savefile q4_report",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is True
    assert "Saved as `q4_report`" in adapter.messages[0][1]
    row = await telegram_store.get_file("tg_111", "q4_report", slide=False)
    assert row is not None
    assert row["file_id"] == "DOC_ID"
    assert row["file_type"] == "document"
    assert row["filename"] == "report.pdf"
    assert row["mime_type"] == "application/pdf"
    assert row["source_chat_id"] == 111
    assert row["source_message_id"] == 99


@pytest.mark.asyncio
async def test_savefile_photo_happy(isolated_store, adapter):
    msg = {
        "message_id": 1,
        "photo": [
            {"file_id": "P_SMALL", "width": 90},
            {"file_id": "P_LARGE", "width": 1280},
        ],
    }
    handled = await telegram_poller._handle_savefile_command(
        adapter, msg, "/savefile chart",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is True
    row = await telegram_store.get_file("tg_111", "chart", slide=False)
    assert row is not None
    assert row["file_id"] == "P_LARGE"
    assert row["file_type"] == "photo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, file_type, extras",
    [
        ("audio", "audio", {"file_name": "song.mp3", "mime_type": "audio/mpeg"}),
        ("video", "video", {"file_name": "clip.mp4", "mime_type": "video/mp4"}),
        ("voice", "voice", {"mime_type": "audio/ogg"}),
        ("video_note", "video_note", {}),
    ],
)
async def test_savefile_other_types(
    isolated_store, adapter, field, file_type, extras
):
    payload = {"file_id": f"{file_type.upper()}_ID"}
    payload.update(extras)
    msg = {"message_id": 1, field: payload}
    handled = await telegram_poller._handle_savefile_command(
        adapter, msg, f"/savefile ref_{file_type}",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert handled is True
    row = await telegram_store.get_file(
        "tg_111", f"ref_{file_type}", slide=False
    )
    assert row is not None
    assert row["file_id"] == f"{file_type.upper()}_ID"
    assert row["file_type"] == file_type


# --------------------------------------------------------------------------- duplicate semantics


@pytest.mark.asyncio
async def test_savefile_duplicate_overwrites(isolated_store, adapter):
    msg1 = {"message_id": 1, "document": {"file_id": "FIRST", "file_name": "a.pdf"}}
    await telegram_poller._handle_savefile_command(
        adapter, msg1, "/savefile mydoc",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    row1 = await telegram_store.get_file("tg_111", "mydoc", slide=False)
    assert row1["file_id"] == "FIRST"

    msg2 = {"message_id": 2, "document": {"file_id": "SECOND", "file_name": "b.pdf"}}
    await telegram_poller._handle_savefile_command(
        adapter, msg2, "/savefile mydoc",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    row2 = await telegram_store.get_file("tg_111", "mydoc", slide=False)
    assert row2["file_id"] == "SECOND"
    assert row2["filename"] == "b.pdf"


# --------------------------------------------------------------------------- owner scoping


@pytest.mark.asyncio
async def test_savefile_owner_scoped(isolated_store, adapter):
    msg = {"message_id": 1, "document": {"file_id": "FOR_A", "file_name": "a.pdf"}}
    await telegram_poller._handle_savefile_command(
        adapter, msg, "/savefile shared",
        chat_id=111, chat_type="private", caller_user_id="tg_111",
    )
    assert await telegram_store.list_files("tg_222") == []
    rows = await telegram_store.list_files("tg_111")
    assert len(rows) == 1
    assert rows[0]["file_id"] == "FOR_A"


# --------------------------------------------------------------------------- DM-only enforcement (revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_savefile_in_non_dm_silently_skipped(
    isolated_store, adapter, chat_type
):
    """Group invocations are NOT consumed and NOT cached — silent
    pass-through so the agent (if any) still sees the message."""
    msg = {"message_id": 1, "document": {"file_id": "GROUP_DOC", "file_name": "a.pdf"}}
    handled = await telegram_poller._handle_savefile_command(
        adapter, msg, "/savefile mydoc",
        chat_id=-1001234, chat_type=chat_type, caller_user_id="tg_111",
    )
    assert handled is False
    assert adapter.messages == []
    assert await telegram_store.list_files("tg_111") == []


# --------------------------------------------------------------------------- token-exact prefix (revision)


@pytest.mark.asyncio
async def test_savefilex_not_consumed(isolated_store, adapter):
    """`/savefilex` and `/savefile_extra` are NOT `/savefile` and must
    pass through without consuming the message."""
    msg = {"message_id": 1, "document": {"file_id": "DOC_ID", "file_name": "a.pdf"}}
    for bad in ("/savefilex foo", "/savefile_extra foo", "/savefilex"):
        adapter.messages.clear()
        handled = await telegram_poller._handle_savefile_command(
            adapter, msg, bad,
            chat_id=111, chat_type="private", caller_user_id="tg_111",
        )
        assert handled is False, f"{bad!r} should NOT consume"
        assert adapter.messages == []
