"""Unit tests for TelegramAdapter strict variants (proposal §7.9).

Covers:
    - non-200 + Telegram error body → strict variant returns the dict,
      never None.
    - 200 success → {"ok": True, "message_id": ..., ...}.
    - send_media_strict extracts the right file_id per type for all six
      allowed file_types.
    - send_media_strict(file_id=..., file_type='voice') posts to sendVoice
      (NOT sendAudio) — voice/audio disambiguation.
    - send_media_strict(file_id=..., file_type='video_note') posts to
      sendVideoNote (NOT sendVideo) — video_note/video disambiguation.
    - send_media_strict(file_id=..., file_type=None) returns error
      ("file_type required when sending by file_id"); strict adapter
      refuses to guess.
    - Bytes upload (file_id is None) with file_type=None falls back to
      MIME-prefix mapping (existing telegram_poller.py:211-218 semantics).
    - Cache write side-effect: when file_ref + owner_user_id are passed,
      a row lands in outbound_files (slice 2 store).
"""

from __future__ import annotations

import json as _json
from typing import Any

import httpx
import pytest

from app.core import telegram_store
from interfaces import telegram_poller


# --------------------------------------------------------------------------- fakes


class _FakeRequest:
    """Captured POST: (url, json, data, files)."""

    def __init__(self, url, json=None, data=None, files=None):
        self.url = url
        # Extract method tail (.../bot<TOK>/<method>).
        self.method_name = url.rsplit("/", 1)[-1]
        self.json = json
        self.data = data
        # Files captured shape: {field_name: (filename, bytes, mime)}.
        self.files = files


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: dict | None = None,
        text_body: str | None = None,
    ):
        self.status_code = status_code
        self._body = body
        self.text = text_body or (_json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class _FakeClient:
    """Captures every POST and returns a queued response.

    The queue is keyed by Telegram method name so a single test can wire
    different responses to e.g. sendMessage and sendPhoto.
    """

    def __init__(self):
        self.requests: list[_FakeRequest] = []
        self._responses: dict[str, list[_FakeResponse]] = {}
        self._default_response = _FakeResponse(
            200, {"ok": True, "result": {"message_id": 1, "chat": {"id": -100}}}
        )

    def queue(self, method_name: str, response: _FakeResponse):
        self._responses.setdefault(method_name, []).append(response)

    async def post(self, url, *, json=None, data=None, files=None):
        req = _FakeRequest(url, json=json, data=data, files=files)
        self.requests.append(req)
        queue = self._responses.get(req.method_name) or []
        if queue:
            return queue.pop(0)
        return self._default_response


@pytest.fixture
def fake_client(monkeypatch):
    return _FakeClient()


@pytest.fixture
def adapter(fake_client):
    return telegram_poller.TelegramAdapter(fake_client, "TEST-TOKEN")


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Telegram store points at a per-test DB; cache write side-effects
    don't bleed across tests."""
    monkeypatch.setattr(
        telegram_store, "DB_PATH", str(tmp_path / "telegram_skills.db")
    )
    monkeypatch.delenv("TELEGRAM_FILE_CACHE_TTL_HOURS", raising=False)
    telegram_store._reset_for_tests()
    return tmp_path


# --------------------------------------------------------------------------- send_text_strict


@pytest.mark.asyncio
async def test_send_text_strict_success(adapter, fake_client):
    fake_client.queue(
        "sendMessage",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 42,
                    "chat": {"id": -1001234},
                },
            },
        ),
    )
    result = await adapter.send_text_strict(-1001234, "hello")
    assert result == {"ok": True, "message_id": 42, "chat_id": -1001234}
    # One request — markdown path succeeded; no fallback needed.
    assert len(fake_client.requests) == 1
    assert fake_client.requests[0].json["parse_mode"] == "Markdown"


@pytest.mark.asyncio
async def test_send_text_strict_markdown_fallback(adapter, fake_client):
    # First call (markdown) fails; second call (plain) succeeds.
    fake_client.queue(
        "sendMessage",
        _FakeResponse(
            400, {"ok": False, "error_code": 400, "description": "can't parse entities"}
        ),
    )
    fake_client.queue(
        "sendMessage",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {"message_id": 7, "chat": {"id": -1}},
            },
        ),
    )
    result = await adapter.send_text_strict(-1, "raw_underscored_text")
    assert result["ok"] is True
    assert len(fake_client.requests) == 2
    assert fake_client.requests[1].json.get("parse_mode") is None


@pytest.mark.asyncio
async def test_send_text_strict_verbatim_error(adapter, fake_client):
    # Both markdown + plain-text retries fail; description must surface verbatim.
    err_body = {
        "ok": False,
        "error_code": 403,
        "description": "Forbidden: bot is not a member of the supergroup chat",
    }
    fake_client.queue("sendMessage", _FakeResponse(403, err_body))
    fake_client.queue("sendMessage", _FakeResponse(403, err_body))
    result = await adapter.send_text_strict(-1001234, "hi")
    assert result == {
        "ok": False,
        "error_code": 403,
        "description": "Forbidden: bot is not a member of the supergroup chat",
    }
    # NEVER None.
    assert result is not None


@pytest.mark.asyncio
async def test_send_text_strict_oversize_rejected(adapter, fake_client):
    huge = "x" * 4097
    result = await adapter.send_text_strict(-1, huge)
    assert result["ok"] is False
    assert "4096" in result["description"]
    assert fake_client.requests == []  # no network call attempted


@pytest.mark.asyncio
async def test_send_text_strict_http_transport_error(adapter, fake_client):
    async def boom(*a, **kw):
        raise httpx.ConnectError("simulated network drop")

    fake_client.post = boom  # type: ignore[assignment]
    result = await adapter.send_text_strict(-1, "hi")
    assert result["ok"] is False
    assert "transport error" in result["description"]


@pytest.mark.asyncio
async def test_send_text_strict_scrubs_bot_token_from_transport_error(
    adapter, fake_client, monkeypatch
):
    """httpx exceptions can echo the request URL, which contains the bot
    token. The strict-variant description is user-visible — must be
    scrubbed."""
    real_token = "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", real_token)

    async def boom(*a, **kw):
        raise httpx.ConnectError(
            f"connect failed for https://api.telegram.org/bot{real_token}/sendMessage"
        )

    fake_client.post = boom  # type: ignore[assignment]
    result = await adapter.send_text_strict(-1, "hi")
    assert result["ok"] is False
    # Token MUST NOT appear in the description that flows back to users.
    assert real_token not in result["description"]
    assert "[REDACTED]" in result["description"]


@pytest.mark.asyncio
async def test_send_text_strict_scrubs_token_from_non_json_response(
    adapter, fake_client, monkeypatch
):
    """A non-JSON server reply that happens to echo the request URL must
    also be scrubbed before reaching the user."""
    real_token = "9876543210:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", real_token)

    fake_client.queue(
        "sendMessage",
        _FakeResponse(
            502,
            body=None,
            text_body=(
                "<html><body>upstream timeout while proxying "
                f"https://api.telegram.org/bot{real_token}/sendMessage"
                "</body></html>"
            ),
        ),
    )
    # Second retry (no markdown) — same error response.
    fake_client.queue(
        "sendMessage",
        _FakeResponse(
            502,
            body=None,
            text_body=(
                f"... bot{real_token} ..."
            ),
        ),
    )

    result = await adapter.send_text_strict(-1, "hi")
    assert result["ok"] is False
    assert real_token not in result["description"]
    assert "[REDACTED]" in result["description"]


# --------------------------------------------------------------------------- send_media_strict — by file_id (re-send)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_type, method, field",
    [
        ("photo", "sendPhoto", "photo"),
        ("document", "sendDocument", "document"),
        ("audio", "sendAudio", "audio"),
        ("video", "sendVideo", "video"),
        ("voice", "sendVoice", "voice"),
        ("video_note", "sendVideoNote", "video_note"),
    ],
)
async def test_send_media_strict_by_file_id_routes_to_correct_method(
    adapter, fake_client, isolated_store, file_type, method, field
):
    # Per-type success body — photo returns a list, others a dict.
    if file_type == "photo":
        result_obj: dict[str, Any] = {
            "message_id": 5,
            "chat": {"id": -1001234},
            "photo": [
                {"file_id": "PHOTO_SMALL", "width": 90},
                {"file_id": "PHOTO_LARGE", "width": 1280},
            ],
        }
        expected_returned_file_id = "PHOTO_LARGE"
    else:
        result_obj = {
            "message_id": 5,
            "chat": {"id": -1001234},
            field: {"file_id": f"{file_type.upper()}_ID"},
        }
        expected_returned_file_id = f"{file_type.upper()}_ID"

    fake_client.queue(method, _FakeResponse(200, {"ok": True, "result": result_obj}))

    out = await adapter.send_media_strict(
        target_id=-1001234,
        data=None,
        mime_type="",
        file_id=f"CACHED_{file_type.upper()}",
        file_type=file_type,
    )

    assert out["ok"] is True
    assert out["file_type"] == file_type
    assert out["file_id"] == expected_returned_file_id
    assert out["message_id"] == 5

    # Confirm the right Bot API method was hit AND the file_id was passed
    # under the correct field name (no MIME guessing involved).
    assert len(fake_client.requests) == 1
    req = fake_client.requests[0]
    assert req.method_name == method
    assert req.json[field] == f"CACHED_{file_type.upper()}"


@pytest.mark.asyncio
async def test_voice_vs_audio_disambiguation(adapter, fake_client, isolated_store):
    """file_type='voice' must hit sendVoice, NOT sendAudio."""
    fake_client.queue(
        "sendVoice",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "voice": {"file_id": "VOICE_ID"},
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=None,
        mime_type="audio/ogg",  # MIME would steer us toward audio; file_type overrides
        file_id="CACHED_VOICE",
        file_type="voice",
    )
    assert out["ok"] is True
    assert fake_client.requests[0].method_name == "sendVoice"
    assert fake_client.requests[0].json["voice"] == "CACHED_VOICE"


@pytest.mark.asyncio
async def test_video_note_vs_video_disambiguation(
    adapter, fake_client, isolated_store
):
    """file_type='video_note' must hit sendVideoNote, NOT sendVideo."""
    fake_client.queue(
        "sendVideoNote",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "video_note": {"file_id": "VN_ID"},
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=None,
        mime_type="video/mp4",
        file_id="CACHED_VN",
        file_type="video_note",
    )
    assert out["ok"] is True
    assert fake_client.requests[0].method_name == "sendVideoNote"
    assert fake_client.requests[0].json["video_note"] == "CACHED_VN"


@pytest.mark.asyncio
async def test_send_media_strict_file_id_without_type_refuses(
    adapter, fake_client, isolated_store
):
    out = await adapter.send_media_strict(
        target_id=-1,
        data=None,
        mime_type="audio/ogg",
        file_id="CACHED_AMBIGUOUS",
        file_type=None,
    )
    assert out["ok"] is False
    assert "file_type required" in out["description"]
    # No network call attempted — refusal is pre-flight.
    assert fake_client.requests == []


@pytest.mark.asyncio
async def test_send_media_strict_neither_data_nor_file_id(
    adapter, fake_client, isolated_store
):
    out = await adapter.send_media_strict(
        target_id=-1,
        data=None,
        mime_type="image/png",
        file_id=None,
        file_type=None,
    )
    assert out["ok"] is False
    assert "either file_id or data" in out["description"]


# --------------------------------------------------------------------------- send_media_strict — bytes upload


@pytest.mark.asyncio
async def test_bytes_upload_mime_fallback_picks_photo(
    adapter, fake_client, isolated_store
):
    fake_client.queue(
        "sendPhoto",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 9,
                    "chat": {"id": -1},
                    "photo": [{"file_id": "P0"}, {"file_id": "P_BIG"}],
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=b"\x89PNGfake",
        mime_type="image/png",
    )
    assert out["ok"] is True
    assert out["file_type"] == "photo"
    assert out["file_id"] == "P_BIG"
    req = fake_client.requests[0]
    assert req.method_name == "sendPhoto"
    # Multipart form was used: files dict non-empty, no JSON body.
    assert req.files is not None
    assert "photo" in req.files
    assert req.json is None


@pytest.mark.asyncio
async def test_bytes_upload_falls_back_to_document_for_unknown_mime(
    adapter, fake_client, isolated_store
):
    fake_client.queue(
        "sendDocument",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "document": {"file_id": "DOC_ID"},
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=b"binary",
        mime_type="application/x-weird",
    )
    assert out["ok"] is True
    assert out["file_type"] == "document"
    assert fake_client.requests[0].method_name == "sendDocument"


@pytest.mark.asyncio
async def test_bytes_upload_with_explicit_file_type_overrides_mime(
    adapter, fake_client, isolated_store
):
    fake_client.queue(
        "sendVoice",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "voice": {"file_id": "VC"},
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=b"opus-bytes",
        mime_type="audio/ogg",
        file_type="voice",
    )
    assert out["ok"] is True
    assert out["file_type"] == "voice"
    assert fake_client.requests[0].method_name == "sendVoice"


# --------------------------------------------------------------------------- cache write side-effect


@pytest.mark.asyncio
async def test_send_media_strict_writes_cache_row_when_file_ref_supplied(
    adapter, fake_client, isolated_store
):
    fake_client.queue(
        "sendPhoto",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 11,
                    "chat": {"id": -1001234},
                    "photo": [{"file_id": "P0"}, {"file_id": "P_BIG"}],
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1001234,
        data=b"png",
        mime_type="image/png",
        file_ref="q1_revenue",
        owner_user_id="tg_111",
    )
    assert out["ok"] is True

    cached = await telegram_store.get_file("tg_111", "q1_revenue", slide=False)
    assert cached is not None
    assert cached["file_id"] == "P_BIG"
    assert cached["file_type"] == "photo"
    assert cached["source_chat_id"] == -1001234
    assert cached["source_message_id"] == 11


@pytest.mark.asyncio
async def test_send_media_strict_auto_derives_file_ref_from_file_path(
    adapter, fake_client, isolated_store
):
    fake_client.queue(
        "sendDocument",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "document": {"file_id": "DOC_ID"},
                },
            },
        ),
    )
    await adapter.send_media_strict(
        target_id=-1,
        data=b"...",
        mime_type="application/pdf",
        file_path="/tmp/Q4 Report.pdf",
        owner_user_id="tg_222",
    )
    # Auto-derived file_ref = "q4 report" (case-folded basename minus ext).
    cached = await telegram_store.get_file("tg_222", "q4 report", slide=False)
    assert cached is not None
    assert cached["file_id"] == "DOC_ID"


@pytest.mark.asyncio
async def test_send_media_strict_no_cache_when_owner_missing(
    adapter, fake_client, isolated_store
):
    fake_client.queue(
        "sendDocument",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "document": {"file_id": "DOC_ID"},
                },
            },
        ),
    )
    await adapter.send_media_strict(
        target_id=-1,
        data=b"...",
        mime_type="application/pdf",
        file_ref="my_doc",
        # owner_user_id intentionally absent
    )
    # No cache row should exist.
    listed = await telegram_store.list_files("")
    assert listed == []
    listed_other = await telegram_store.list_files("tg_111")
    assert listed_other == []


# --------------------------------------------------------------------------- copy_message_strict


@pytest.mark.asyncio
async def test_photo_extractor_tolerates_missing_file_id(
    adapter, fake_client, isolated_store
):
    """sendPhoto 200 with a malformed photo variant (no file_id key) must
    NOT raise — strict return shape required (ok:True, file_id='')."""
    fake_client.queue(
        "sendPhoto",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "photo": [{"width": 1}],  # NO file_id
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=b"...",
        mime_type="image/png",
    )
    assert out["ok"] is True
    assert out["file_id"] == ""


@pytest.mark.asyncio
async def test_photo_extractor_tolerates_non_dict_variant(
    adapter, fake_client, isolated_store
):
    """photo[-1] not a dict shouldn't crash either."""
    fake_client.queue(
        "sendPhoto",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "photo": ["unexpected-string-instead-of-dict"],
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=b"...",
        mime_type="image/png",
    )
    assert out["ok"] is True
    assert out["file_id"] == ""


@pytest.mark.asyncio
async def test_photo_extractor_tolerates_empty_list(
    adapter, fake_client, isolated_store
):
    """sendPhoto with photo:[] (already handled, but pin it)."""
    fake_client.queue(
        "sendPhoto",
        _FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "message_id": 1,
                    "chat": {"id": -1},
                    "photo": [],
                },
            },
        ),
    )
    out = await adapter.send_media_strict(
        target_id=-1,
        data=b"...",
        mime_type="image/png",
    )
    assert out["ok"] is True
    assert out["file_id"] == ""


@pytest.mark.asyncio
async def test_copy_message_strict_success(adapter, fake_client):
    fake_client.queue(
        "copyMessage",
        _FakeResponse(200, {"ok": True, "result": {"message_id": 99}}),
    )
    out = await adapter.copy_message_strict(
        target_id=-1001234, from_chat_id=-1005555, message_id=42
    )
    assert out["ok"] is True
    assert out["message_id"] == 99


@pytest.mark.asyncio
async def test_copy_message_strict_failure_surfaces_verbatim(adapter, fake_client):
    fake_client.queue(
        "copyMessage",
        _FakeResponse(
            400,
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: message to copy not found",
            },
        ),
    )
    out = await adapter.copy_message_strict(
        target_id=-1, from_chat_id=-2, message_id=1
    )
    assert out["ok"] is False
    assert out["description"] == "Bad Request: message to copy not found"
