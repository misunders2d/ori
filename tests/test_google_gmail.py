"""Tests for Gmail read-only tools — helpers and tool surface."""

import base64
import os
import time as time_mod
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tools.google_gmail import _decode_body, _sweep_attachments, _truncate
from app.tools.google_oauth.device_flow import SCOPES


def _mock_response(json_data: dict, status: int = 200):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    return resp


def _mock_async_client(get_responses=None, post_responses=None):
    """Build a mock httpx.AsyncClient class that yields given responses from get/post."""
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    if get_responses is not None:
        client.get = AsyncMock(side_effect=list(get_responses))
    if post_responses is not None:
        client.post = AsyncMock(side_effect=list(post_responses))
    client_cls = MagicMock(return_value=client)
    return client_cls, client


def _mock_ctx(email: str = "user@test.com"):
    ctx = MagicMock()
    ctx.state.to_dict.return_value = {"user_id": email}
    return ctx


def test_gmail_readonly_scope_present():
    assert "https://www.googleapis.com/auth/gmail.readonly" in SCOPES


def _b64url(s: str) -> str:
    """Encode a string the way Gmail does — urlsafe base64."""
    return base64.urlsafe_b64encode(s.encode()).decode()


def test_decode_body_plain_text_single_part():
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _b64url("Hello world")},
    }
    assert _decode_body(payload) == "Hello world"


def test_decode_body_prefers_plain_over_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64url("<p>HTML version</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64url("Plain version")}},
        ],
    }
    assert _decode_body(payload) == "Plain version"


def test_decode_body_html_fallback_strips_tags():
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64url("<html><body><p>Hi <b>there</b></p></body></html>")},
    }
    result = _decode_body(payload)
    assert "<" not in result
    assert "Hi" in result
    assert "there" in result


def test_decode_body_nested_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url("Nested plain")}},
                ],
            },
            {"mimeType": "application/pdf", "filename": "attach.pdf", "body": {"attachmentId": "x"}},
        ],
    }
    assert _decode_body(payload) == "Nested plain"


def test_decode_body_empty_returns_empty_string():
    assert _decode_body({"mimeType": "image/png", "body": {"attachmentId": "x"}}) == ""


def test_truncate_under_threshold_returns_unchanged():
    body = "short body"
    assert _truncate(body, full=False) == body


def test_truncate_over_threshold_clips_with_suffix():
    body = "x" * 9000
    result = _truncate(body, full=False)
    assert result.startswith("x" * 8000)
    assert "[truncated" in result
    assert "1000 more chars" in result
    assert "full=True" in result


def test_truncate_full_true_returns_unchanged_over_threshold():
    body = "x" * 9000
    assert _truncate(body, full=True) == body


def test_truncate_exactly_at_threshold_unchanged():
    body = "x" * 8000
    assert _truncate(body, full=False) == body


def test_sweep_missing_dir_is_noop(tmp_path):
    missing = str(tmp_path / "does_not_exist")
    with patch("app.tools.google_gmail._ATTACHMENT_DIR", missing):
        _sweep_attachments()  # must not raise
    assert not os.path.exists(missing)


def test_sweep_removes_expired_files(tmp_path):
    d = tmp_path / "attach"
    d.mkdir()
    old = d / "old.bin"
    fresh = d / "fresh.bin"
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    old_mtime = time_mod.time() - 48 * 3600
    os.utime(old, (old_mtime, old_mtime))
    with patch("app.tools.google_gmail._ATTACHMENT_DIR", str(d)):
        _sweep_attachments()
    assert not old.exists()
    assert fresh.exists()


def test_sweep_respects_env_ttl_override(tmp_path):
    d = tmp_path / "attach"
    d.mkdir()
    f = d / "file.bin"
    f.write_bytes(b"data")
    two_h_ago = time_mod.time() - 2 * 3600
    os.utime(f, (two_h_ago, two_h_ago))
    with patch("app.tools.google_gmail._ATTACHMENT_DIR", str(d)), \
         patch.dict(os.environ, {"GMAIL_ATTACHMENT_TTL_HOURS": "1"}):
        _sweep_attachments()
    assert not f.exists()


@pytest.mark.asyncio
async def test_gmail_list_labels_not_connected():
    from app.tools.google_gmail import gmail_list_labels
    with patch("app.tools.google_gmail.get_token", return_value=None):
        result = await gmail_list_labels(tool_context=_mock_ctx())
    assert result["status"] == "error"
    assert "not connected" in result["message"].lower()


def _sample_message(body_text: str, attachments: list[dict] | None = None) -> dict:
    parts = [
        {"mimeType": "text/plain", "body": {"data": _b64url(body_text)}},
    ]
    for a in attachments or []:
        parts.append({
            "mimeType": a.get("mime", "application/octet-stream"),
            "filename": a.get("filename", "file.bin"),
            "body": {"attachmentId": a.get("id", "att_x"), "size": a.get("size", 0)},
        })
    return {
        "id": "msg_1",
        "threadId": "thr_1",
        "payload": {
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Tue, 21 Apr 2026 10:00:00 -0400"},
            ],
            "mimeType": "multipart/mixed",
            "parts": parts,
        },
    }


@pytest.mark.asyncio
async def test_gmail_list_labels_happy_path():
    from app.tools.google_gmail import gmail_list_labels
    api_response = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_1", "name": "Suppliers", "type": "user"},
        ]
    }
    client_cls, _ = _mock_async_client(get_responses=[_mock_response(api_response)])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_list_labels(tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["count"] == 2
    assert result["labels"] == [
        {"id": "INBOX", "name": "INBOX", "type": "system"},
        {"id": "Label_1", "name": "Suppliers", "type": "user"},
    ]


@pytest.mark.asyncio
async def test_gmail_get_message_not_connected():
    from app.tools.google_gmail import gmail_get_message
    with patch("app.tools.google_gmail.get_token", return_value=None):
        result = await gmail_get_message("msg_1", tool_context=_mock_ctx())
    assert result["status"] == "error"
    assert "not connected" in result["message"].lower()


@pytest.mark.asyncio
async def test_gmail_get_message_happy_path_with_attachment():
    from app.tools.google_gmail import gmail_get_message
    sample = _sample_message(
        "Hello world",
        attachments=[{"filename": "invoice.pdf", "mime": "application/pdf", "id": "att_1", "size": 12345}],
    )
    client_cls, _ = _mock_async_client(get_responses=[_mock_response(sample)])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_get_message("msg_1", tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["id"] == "msg_1"
    assert result["thread_id"] == "thr_1"
    assert result["body"] == "Hello world"
    assert result["headers"]["from"] == "alice@example.com"
    assert result["headers"]["subject"] == "Hello"
    assert len(result["attachments"]) == 1
    att = result["attachments"][0]
    assert att["filename"] == "invoice.pdf"
    assert att["mime"] == "application/pdf"
    assert att["id"] == "att_1"
    assert att["size"] == 12345


@pytest.mark.asyncio
async def test_gmail_get_message_truncates_long_body():
    from app.tools.google_gmail import gmail_get_message
    long_body = "x" * 9000
    sample = _sample_message(long_body)
    client_cls, _ = _mock_async_client(get_responses=[_mock_response(sample)])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_get_message("msg_1", tool_context=_mock_ctx())
    assert "[truncated" in result["body"]
    assert len(result["body"]) < len(long_body)


@pytest.mark.asyncio
async def test_gmail_get_message_full_true_returns_untruncated():
    from app.tools.google_gmail import gmail_get_message
    long_body = "x" * 9000
    sample = _sample_message(long_body)
    client_cls, _ = _mock_async_client(get_responses=[_mock_response(sample)])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_get_message("msg_1", full=True, tool_context=_mock_ctx())
    assert result["body"] == long_body


def _metadata_message(id_: str, subject: str, from_: str, has_attachment: bool = False) -> dict:
    headers = [
        {"name": "From", "value": from_},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Tue, 21 Apr 2026 10:00:00 -0400"},
    ]
    parts = []
    if has_attachment:
        parts.append({
            "mimeType": "application/pdf",
            "filename": "file.pdf",
            "body": {"attachmentId": "att_x", "size": 1},
        })
    return {
        "id": id_,
        "threadId": f"thr_{id_}",
        "snippet": f"snippet for {id_}",
        "payload": {
            "headers": headers,
            "mimeType": "multipart/mixed" if parts else "text/plain",
            "parts": parts,
        },
    }


@pytest.mark.asyncio
async def test_gmail_list_messages_not_connected():
    from app.tools.google_gmail import gmail_list_messages
    with patch("app.tools.google_gmail.get_token", return_value=None):
        result = await gmail_list_messages(tool_context=_mock_ctx())
    assert result["status"] == "error"
    assert "not connected" in result["message"].lower()


@pytest.mark.asyncio
async def test_gmail_list_messages_happy_path_with_enrichment():
    from app.tools.google_gmail import gmail_list_messages
    list_resp = _mock_response({
        "messages": [{"id": "m1", "threadId": "thr_m1"}, {"id": "m2", "threadId": "thr_m2"}],
    })
    m1 = _mock_response(_metadata_message("m1", "Order update", "amazon@amazon.com", has_attachment=True))
    m2 = _mock_response(_metadata_message("m2", "Supplier invoice", "supplier@example.com"))
    client_cls, _ = _mock_async_client(get_responses=[list_resp, m1, m2])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_list_messages(query="is:unread", max_results=2, tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["count"] == 2
    assert [m["id"] for m in result["messages"]] == ["m1", "m2"]
    assert result["messages"][0]["from"] == "amazon@amazon.com"
    assert result["messages"][0]["subject"] == "Order update"
    assert result["messages"][0]["has_attachments"] is True
    assert result["messages"][0]["thread_id"] == "thr_m1"
    assert result["messages"][1]["has_attachments"] is False


@pytest.mark.asyncio
async def test_gmail_list_messages_empty_result():
    from app.tools.google_gmail import gmail_list_messages
    list_resp = _mock_response({})
    client_cls, _ = _mock_async_client(get_responses=[list_resp])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_list_messages(tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["messages"] == []


@pytest.mark.asyncio
async def test_gmail_get_thread_not_connected():
    from app.tools.google_gmail import gmail_get_thread
    with patch("app.tools.google_gmail.get_token", return_value=None):
        result = await gmail_get_thread("thr_1", tool_context=_mock_ctx())
    assert result["status"] == "error"
    assert "not connected" in result["message"].lower()


@pytest.mark.asyncio
async def test_gmail_get_thread_happy_path():
    from app.tools.google_gmail import gmail_get_thread
    thread_resp = _mock_response({
        "id": "thr_1",
        "messages": [
            _sample_message("First message"),
            _sample_message("Second message"),
        ],
    })
    client_cls, _ = _mock_async_client(get_responses=[thread_resp])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_get_thread("thr_1", tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["id"] == "thr_1"
    assert len(result["messages"]) == 2
    assert result["messages"][0]["body"] == "First message"
    assert result["messages"][1]["body"] == "Second message"


@pytest.mark.asyncio
async def test_gmail_get_thread_truncates_each_message():
    from app.tools.google_gmail import gmail_get_thread
    long_body = "y" * 9000
    thread_resp = _mock_response({
        "id": "thr_1",
        "messages": [_sample_message(long_body), _sample_message(long_body)],
    })
    client_cls, _ = _mock_async_client(get_responses=[thread_resp])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_get_thread("thr_1", tool_context=_mock_ctx())
    assert all("[truncated" in m["body"] for m in result["messages"])


def _thread_metadata(id_: str, froms: list[str], last_date: str) -> dict:
    messages = [
        {
            "id": f"{id_}_msg_{i}",
            "payload": {"headers": [
                {"name": "From", "value": f},
                {"name": "Date", "value": last_date if i == len(froms) - 1 else "Mon, 20 Apr 2026 10:00:00 -0400"},
            ]},
        }
        for i, f in enumerate(froms)
    ]
    return {
        "id": id_,
        "snippet": f"snippet for {id_}",
        "messages": messages,
    }


@pytest.mark.asyncio
async def test_gmail_list_threads_not_connected():
    from app.tools.google_gmail import gmail_list_threads
    with patch("app.tools.google_gmail.get_token", return_value=None):
        result = await gmail_list_threads(tool_context=_mock_ctx())
    assert result["status"] == "error"
    assert "not connected" in result["message"].lower()


@pytest.mark.asyncio
async def test_gmail_list_threads_happy_path_with_enrichment():
    from app.tools.google_gmail import gmail_list_threads
    list_resp = _mock_response({"threads": [{"id": "t1"}, {"id": "t2"}]})
    t1 = _mock_response(_thread_metadata(
        "t1",
        ["alice@example.com", "bob@example.com", "alice@example.com"],
        "Tue, 21 Apr 2026 12:00:00 -0400",
    ))
    t2 = _mock_response(_thread_metadata(
        "t2",
        ["supplier@example.com"],
        "Tue, 21 Apr 2026 09:00:00 -0400",
    ))
    client_cls, _ = _mock_async_client(get_responses=[list_resp, t1, t2])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_list_threads(query="is:unread", max_results=2, tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["count"] == 2
    t1_out = result["threads"][0]
    assert t1_out["id"] == "t1"
    assert t1_out["message_count"] == 3
    assert sorted(t1_out["participants"]) == ["alice@example.com", "bob@example.com"]
    assert t1_out["last_date"] == "Tue, 21 Apr 2026 12:00:00 -0400"
    t2_out = result["threads"][1]
    assert t2_out["message_count"] == 1
    assert t2_out["participants"] == ["supplier@example.com"]


@pytest.mark.asyncio
async def test_gmail_list_threads_empty():
    from app.tools.google_gmail import gmail_list_threads
    list_resp = _mock_response({})
    client_cls, _ = _mock_async_client(get_responses=[list_resp])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_list_threads(tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["threads"] == []


@pytest.mark.asyncio
async def test_gmail_download_attachment_not_connected():
    from app.tools.google_gmail import gmail_download_attachment
    with patch("app.tools.google_gmail.get_token", return_value=None):
        result = await gmail_download_attachment("msg_1", "att_1", tool_context=_mock_ctx())
    assert result["status"] == "error"
    assert "not connected" in result["message"].lower()


@pytest.mark.asyncio
async def test_gmail_download_attachment_writes_file_and_sweeps(tmp_path):
    from app.tools.google_gmail import gmail_download_attachment
    content = b"hello pdf bytes"
    att_resp = _mock_response({
        "size": len(content),
        "data": base64.urlsafe_b64encode(content).decode(),
    })
    client_cls, _ = _mock_async_client(get_responses=[att_resp])
    attach_dir = str(tmp_path / "gmail_attachments")
    sweep_calls: list[int] = []

    def fake_sweep():
        sweep_calls.append(1)

    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail._ATTACHMENT_DIR", attach_dir), \
         patch("app.tools.google_gmail._sweep_attachments", fake_sweep), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_download_attachment(
            "msg_1", "att_1", filename="invoice.pdf", tool_context=_mock_ctx(),
        )

    assert sweep_calls == [1]
    assert result["status"] == "success"
    assert result["filename"] == "invoice.pdf"
    assert result["size_bytes"] == len(content)
    assert result["file_path"] == os.path.join(attach_dir, "msg_1_invoice.pdf")
    with open(result["file_path"], "rb") as f:
        assert f.read() == content


@pytest.mark.asyncio
async def test_gmail_download_attachment_default_filename(tmp_path):
    from app.tools.google_gmail import gmail_download_attachment
    att_resp = _mock_response({
        "size": 3,
        "data": base64.urlsafe_b64encode(b"abc").decode(),
    })
    client_cls, _ = _mock_async_client(get_responses=[att_resp])
    attach_dir = str(tmp_path / "gmail_attachments")
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail._ATTACHMENT_DIR", attach_dir), \
         patch("app.tools.google_gmail._sweep_attachments", lambda: None), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_download_attachment("msg_1", "att_1", tool_context=_mock_ctx())
    assert result["filename"] == "att_1"
    assert result["file_path"] == os.path.join(attach_dir, "msg_1_att_1")
