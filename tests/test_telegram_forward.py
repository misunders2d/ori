"""Unit tests for app.tools.telegram.telegram_forward (proposal §7.5).

Covers:
    - cache hit → fake adapter sees send_media_strict(file_id=...,
      file_type=row.file_type).
    - per-file_type routing: parametrized over the six allowed types.
    - voice vs. audio + video_note vs. video disambiguation.
    - cache miss → error.
    - expired → error mentions expiry.
    - file_id rejected → fallback to copy_message_strict.
    - fallback also fails → error verbatim.
    - file_id rejected with no source_chat_id → error suggests re-upload.
    - non-DM target without forward_files capability → error.
    - positive raw chat_id (DM-shaped) → no cap required (v2-c).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from app.core import capabilities, telegram_store, transport
from app.tools import telegram as tg


class _FakeStrictAdapter:
    platform_name = "telegram"

    def __init__(self):
        self.media_calls: list[dict] = []
        self.copy_calls: list[dict] = []
        self.media_response: dict | None = None
        self.copy_response: dict | None = None

    async def send_media_strict(
        self,
        target_id,
        data,
        mime_type,
        caption="",
        *,
        file_id=None,
        file_type=None,
        file_ref=None,
        owner_user_id=None,
        file_path=None,
    ):
        self.media_calls.append(
            {
                "target_id": target_id,
                "data": data,
                "mime_type": mime_type,
                "caption": caption,
                "file_id": file_id,
                "file_type": file_type,
                "file_ref": file_ref,
                "owner_user_id": owner_user_id,
                "file_path": file_path,
            }
        )
        if self.media_response is not None:
            return self.media_response
        return {
            "ok": True,
            "message_id": 42,
            "chat_id": target_id,
            "file_id": file_id,
            "file_type": file_type,
        }

    async def copy_message_strict(self, target_id, from_chat_id, message_id):
        self.copy_calls.append(
            {
                "target_id": target_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
            }
        )
        if self.copy_response is not None:
            return self.copy_response
        return {"ok": True, "message_id": 100, "chat_id": target_id}


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
    monkeypatch.delenv("TELEGRAM_FILE_CACHE_TTL_HOURS", raising=False)
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    telegram_store._reset_for_tests()
    original = dict(transport._registry)
    yield tmp_path
    transport._registry.clear()
    transport._registry.update(original)


@pytest.fixture
def adapter(isolated_env):
    a = _FakeStrictAdapter()
    transport.register_adapter(a)
    return a


# --------------------------------------------------------------------------- happy path


@pytest.mark.asyncio
async def test_cache_hit_routes_through_send_media_strict(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111",
        "q1_revenue",
        "PHOTO_FILE_ID",
        "photo",
        source_chat_id=-1009999,
        source_message_id=42,
    )
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="q1_revenue", target="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    assert result["method"] == "file_id"
    assert result["file_ref"] == "q1_revenue"
    assert result["file_type"] == "photo"
    call = adapter.media_calls[0]
    assert call["target_id"] == -1001234
    assert call["file_id"] == "PHOTO_FILE_ID"
    assert call["file_type"] == "photo"
    assert call["data"] is None  # by-file_id mode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_type",
    sorted(telegram_store.ALLOWED_FILE_TYPES),
)
async def test_per_file_type_routing(adapter, file_type):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111", f"ref_{file_type}", f"FID_{file_type}", file_type
    )
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref=f"ref_{file_type}", target="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    assert adapter.media_calls[0]["file_type"] == file_type
    assert adapter.media_calls[0]["file_id"] == f"FID_{file_type}"


@pytest.mark.asyncio
async def test_voice_disambiguation(adapter):
    """telegram_forward passes row.file_type=='voice' so adapter must
    route to sendVoice path — the cap is for the upstream cache (and
    is pinned in the adapter tests). Here we only confirm the cap arg
    propagates."""
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111", "voice_clip", "VOICE_ID", "voice", mime_type="audio/ogg"
    )
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="voice_clip", target="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    # Critical: file_type=voice, NOT audio (the mime_type would steer
    # the adapter to sendAudio without the explicit file_type).
    assert adapter.media_calls[0]["file_type"] == "voice"
    assert adapter.media_calls[0]["mime_type"] == "audio/ogg"


@pytest.mark.asyncio
async def test_video_note_disambiguation(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111", "vn_clip", "VN_ID", "video_note", mime_type="video/mp4"
    )
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="vn_clip", target="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    assert adapter.media_calls[0]["file_type"] == "video_note"


# --------------------------------------------------------------------------- cache misses


@pytest.mark.asyncio
async def test_cache_miss_returns_error(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="never_seen", target="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "not in cache" in result["message"]
    assert adapter.media_calls == []


@pytest.mark.asyncio
async def test_expired_entry_returns_error(isolated_env, adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111", "stale", "OLD_ID", "document"
    )
    # Backdate expires_at into the past.
    past = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat(timespec="microseconds")
    async with aiosqlite.connect(str(isolated_env / "telegram_skills.db")) as conn:
        await conn.execute(
            "UPDATE outbound_files SET expires_at = ? WHERE file_ref = ?",
            (past, "stale"),
        )
        await conn.commit()

    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="stale", target="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "not in cache" in result["message"]


@pytest.mark.asyncio
async def test_wrong_owner_cannot_access(adapter):
    """Owner A's file_ref is invisible to owner B."""
    await capabilities.grant("tg_222", "forward_files")
    await telegram_store.save_alias(
        "tg_222", "engineering", -1001234, "supergroup"
    )
    # Owner A puts file.
    await telegram_store.put_file(
        "tg_111", "secret", "A_FILE", "document"
    )
    # Owner B forwards it (their context).
    ctx = _ToolContext(user_id="tg_222")
    result = await tg.telegram_forward(
        file_ref="secret", target="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "not in cache" in result["message"]


# --------------------------------------------------------------------------- copyMessage fallback


@pytest.mark.asyncio
async def test_file_id_rejected_falls_back_to_copy_message(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111",
        "chart",
        "STALE_FILE_ID",
        "photo",
        source_chat_id=-1005555,
        source_message_id=99,
    )
    adapter.media_response = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: wrong file identifier/HTTP URL specified",
    }
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="chart", target="engineering", tool_context=ctx
    )
    assert result["status"] == "success"
    assert result["method"] == "copyMessage"
    assert result["primary_error"].startswith("Bad Request")
    # Adapter saw both calls.
    assert len(adapter.media_calls) == 1
    assert adapter.copy_calls == [
        {"target_id": -1001234, "from_chat_id": -1005555, "message_id": 99}
    ]


@pytest.mark.asyncio
async def test_fallback_also_fails_returns_combined_error(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111",
        "chart",
        "STALE_FILE_ID",
        "photo",
        source_chat_id=-1005555,
        source_message_id=99,
    )
    adapter.media_response = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: wrong file identifier",
    }
    adapter.copy_response = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: message to copy not found",
    }
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="chart", target="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "wrong file identifier" in result["message"]
    assert "message to copy not found" in result["message"]


@pytest.mark.asyncio
async def test_file_id_rejected_no_source_means_re_upload(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")
    await telegram_store.put_file(
        "tg_111", "chart", "STALE_FILE_ID", "photo"
        # NO source_chat_id / source_message_id
    )
    adapter.media_response = {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: wrong file identifier",
    }
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="chart", target="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "Re-upload" in result["message"]
    assert adapter.copy_calls == []


# --------------------------------------------------------------------------- capability gating


@pytest.mark.asyncio
async def test_non_dm_target_without_forward_files_blocked(adapter):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await telegram_store.put_file(
        "tg_111", "chart", "FILE_ID", "photo"
    )
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="chart", target="engineering", tool_context=ctx
    )
    assert result["status"] == "error"
    assert "forward_files" in result["message"]
    assert adapter.media_calls == []


@pytest.mark.asyncio
async def test_positive_raw_chat_id_dm_shaped_skips_capability(adapter):
    """Positive raw chat_id is DM-shaped; no cap required. v2-c."""
    await telegram_store.put_file(
        "tg_111", "chart", "FILE_ID", "photo"
    )
    ctx = _ToolContext()
    result = await tg.telegram_forward(
        file_ref="chart", target="555", tool_context=ctx
    )
    assert result["status"] == "success"
    assert adapter.media_calls[0]["target_id"] == 555


@pytest.mark.asyncio
async def test_admin_implicit_bypass_for_forward(adapter, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    await telegram_store.save_alias(
        "tg_admin", "engineering", -1001234, "supergroup"
    )
    await telegram_store.put_file(
        "tg_admin", "chart", "FILE_ID", "photo"
    )
    ctx = _ToolContext(user_id="tg_admin")
    result = await tg.telegram_forward(
        file_ref="chart", target="engineering", tool_context=ctx
    )
    assert result["status"] == "success"


# --------------------------------------------------------------------------- store raises (slice 7 revision)


@pytest.mark.asyncio
async def test_cache_lookup_db_failure_returns_error_dict(
    adapter, monkeypatch, caplog
):
    """If telegram_store.get_file raises (e.g., DB locked), the tool
    must return {status:error} — not propagate the exception."""
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await capabilities.grant("tg_111", "forward_files")

    async def boom(*a, **kw):
        raise OSError("simulated database locked")

    monkeypatch.setattr(telegram_store, "get_file", boom)
    ctx = _ToolContext()
    with caplog.at_level("ERROR", logger="app.tools.telegram"):
        result = await tg.telegram_forward(
            file_ref="anything", target="engineering", tool_context=ctx
        )
    assert result["status"] == "error"
    assert "database locked" in result["message"]
    assert any(
        "telegram_store.get_file failed" in rec.getMessage()
        for rec in caplog.records
    )
    assert adapter.media_calls == []
