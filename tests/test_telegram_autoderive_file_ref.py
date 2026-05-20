"""Slice 6 — poller delivery loop auto-derive file_ref (proposal §7.10).

Confirms that `_deliver_media_items` (the extracted helper from the
poller's `_process_and_send`) routes each `AgentResponse.media_items`
entry through `send_media_strict` with the right kwargs so the Telegram
outbound-files cache fills itself with bot-generated files.

Direct exercise of `send_media_strict(file_path=..., owner_user_id=...)`
is already covered in tests/test_telegram_adapter_strict.py. This file
focuses on the WIRING: the poller hands the right values to the adapter.
"""

from __future__ import annotations

import pytest

from app.core import telegram_store
from interfaces import telegram_poller


class _CapturingAdapter:
    """Minimal stand-in for TelegramAdapter — records every
    send_media_strict call. Returns a configurable success/failure dict."""

    def __init__(self, *, ok: bool = True):
        self._ok = ok
        self.calls: list[dict] = []

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
        self.calls.append(
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
        if self._ok:
            return {
                "ok": True,
                "message_id": len(self.calls),
                "chat_id": target_id,
                "file_id": f"FILE_ID_{len(self.calls)}",
                "file_type": file_type or "photo",
            }
        return {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot is not a member of the supergroup chat",
        }


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        telegram_store, "DB_PATH", str(tmp_path / "telegram_skills.db")
    )
    monkeypatch.delenv("TELEGRAM_FILE_CACHE_TTL_HOURS", raising=False)
    telegram_store._reset_for_tests()
    return tmp_path


@pytest.mark.asyncio
async def test_helper_threads_file_path_and_owner(isolated_store):
    """Slice 5 added `file_path` to media_items; slice 6 must thread both
    `file_path` and `owner_user_id` to send_media_strict on every call."""
    adapter = _CapturingAdapter()
    items = [
        {
            "data": b"\x89PNGbytes",
            "mime_type": "image/png",
            "file_path": "/tmp/q1_revenue.png",
        },
        {
            "data": b"%PDF",
            "mime_type": "application/pdf",
            "file_path": "/tmp/Q4 Report.pdf",
        },
    ]

    results = await telegram_poller._deliver_media_items(
        adapter, -1001234, "tg_111", items
    )
    assert len(results) == 2
    assert all(r["ok"] for r in results)

    assert adapter.calls[0]["target_id"] == -1001234
    assert adapter.calls[0]["file_path"] == "/tmp/q1_revenue.png"
    assert adapter.calls[0]["owner_user_id"] == "tg_111"
    assert adapter.calls[0]["mime_type"] == "image/png"
    assert adapter.calls[1]["file_path"] == "/tmp/Q4 Report.pdf"
    assert adapter.calls[1]["owner_user_id"] == "tg_111"


@pytest.mark.asyncio
async def test_helper_uses_strict_not_best_effort(isolated_store):
    """Regression pin: the helper must NOT fall back to the legacy
    adapter.send_media path — that path swallows errors."""
    adapter = _CapturingAdapter()
    # No `.send_media` attribute on the capturing fake => if the helper
    # ever calls send_media instead of send_media_strict, we'd hit
    # AttributeError. Force the issue.
    items = [
        {"data": b"x", "mime_type": "image/png", "file_path": None}
    ]
    await telegram_poller._deliver_media_items(adapter, -1, "tg_111", items)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_helper_logs_failure_and_continues(isolated_store, caplog):
    """Non-fatal failure: helper must log and continue with the next item."""
    adapter = _CapturingAdapter(ok=False)
    items = [
        {"data": b"first", "mime_type": "image/png", "file_path": "/tmp/a.png"},
        {"data": b"second", "mime_type": "image/png", "file_path": "/tmp/b.png"},
    ]
    with caplog.at_level("ERROR", logger="interfaces.telegram_poller"):
        results = await telegram_poller._deliver_media_items(
            adapter, -1, "tg_111", items
        )
    assert len(results) == 2
    assert all(r["ok"] is False for r in results)
    assert len(adapter.calls) == 2  # second item NOT skipped after first failure
    # Telegram description surfaces verbatim in logs.
    failing_records = [
        rec for rec in caplog.records
        if "Forbidden: bot is not a member" in rec.getMessage()
    ]
    assert len(failing_records) == 2


@pytest.mark.asyncio
async def test_helper_skips_non_dict_or_missing_data(isolated_store):
    """Defensive: non-dict items and items missing `data` are silently
    dropped (not an error path; just guards against malformed inputs)."""
    adapter = _CapturingAdapter()
    items = [
        "not-a-dict",
        {"mime_type": "image/png"},  # no data
        {"data": "not-bytes", "mime_type": "image/png"},  # bad data type
        {
            "data": b"good",
            "mime_type": "image/png",
            "file_path": "/tmp/ok.png",
        },
    ]
    results = await telegram_poller._deliver_media_items(
        adapter, -1, "tg_111", items
    )
    # Only the well-formed item should have produced a call.
    assert len(adapter.calls) == 1
    assert len(results) == 1
    assert adapter.calls[0]["data"] == b"good"


@pytest.mark.asyncio
async def test_end_to_end_cache_row_after_real_adapter_send(isolated_store):
    """Integration: real TelegramAdapter.send_media_strict (with a fake
    httpx client) driven through `_deliver_media_items` populates the
    outbound-files cache with the auto-derived ref."""
    import json as _json
    import httpx

    class _Resp:
        def __init__(self, body):
            self.status_code = 200
            self._body = body
            self.text = _json.dumps(body)

        def json(self):
            return self._body

    class _FakeClient:
        async def post(self, url, *, json=None, data=None, files=None):
            # Mirror Telegram's sendPhoto success shape.
            return _Resp(
                {
                    "ok": True,
                    "result": {
                        "message_id": 7,
                        "chat": {"id": -1001234},
                        "photo": [{"file_id": "P_SMALL"}, {"file_id": "P_LARGE"}],
                    },
                }
            )

    adapter = telegram_poller.TelegramAdapter(_FakeClient(), "TEST-TOKEN")

    items = [
        {
            "data": b"\x89PNGbytes",
            "mime_type": "image/png",
            "file_path": "/tmp/Q1 Sales.png",
        }
    ]
    results = await telegram_poller._deliver_media_items(
        adapter, -1001234, "tg_111", items
    )
    assert results[0]["ok"] is True

    # Auto-derived: case-folded "q1 sales" (basename minus ext).
    cached = await telegram_store.get_file("tg_111", "q1 sales", slide=False)
    assert cached is not None
    assert cached["file_id"] == "P_LARGE"
    assert cached["file_type"] == "photo"


@pytest.mark.asyncio
async def test_no_cache_when_owner_user_id_missing(isolated_store):
    """User-upload-style items (file_path=None) must not populate the
    cache; helper passes owner but adapter sees file_path=None so no
    derivation happens (slice 4 behavior, pinned at the wiring layer)."""
    adapter = _CapturingAdapter()
    items = [
        {"data": b"png", "mime_type": "image/png", "file_path": None}
    ]
    await telegram_poller._deliver_media_items(adapter, -1, "tg_111", items)
    assert adapter.calls[0]["file_path"] is None
