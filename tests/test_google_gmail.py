"""Tests for Gmail read-only tools — helpers and tool surface."""

import base64
import os
import time as time_mod
from unittest.mock import patch

from app.tools.google_gmail import _decode_body, _sweep_attachments, _truncate
from app.tools.google_oauth.device_flow import SCOPES


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
