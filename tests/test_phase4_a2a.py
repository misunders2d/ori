"""Phase 4 unit tests: A2A media (outbound FilePart) + hardening.

Covers:
- _scan_text_for_secrets catches hardcoded patterns + live env values.
- _build_a2a_parts produces correct FilePart shape, enforces inline cap
  and MIME allowlist.
- _send_a2a_message refuses on secret findings.
- _resolve_caller_id_from_context extracts from tool_context.state.
- _validate_peer_url SSRF guard (loopback, private IP, scheme, public).
"""

import base64
import os

import pytest


# ---------------------------------------------------------------------------
# _scan_text_for_secrets
# ---------------------------------------------------------------------------


def test_scan_text_clean_text_no_findings():
    from app.tools.a2a import _scan_text_for_secrets

    assert _scan_text_for_secrets("hello, how are you?") == []


def test_scan_text_catches_slack_bot_token():
    from app.tools.a2a import _scan_text_for_secrets

    findings = _scan_text_for_secrets(
        "Hey, here is the token: xoxb-1234567890-ABCDEFGHIJK please don't share"
    )
    assert any("xoxb" in f for f in findings)


def test_scan_text_catches_openai_key():
    from app.tools.a2a import _scan_text_for_secrets

    findings = _scan_text_for_secrets("api key sk-aaaaaaaaaaaaaaaaaaaa")
    assert any("sk-" in f for f in findings)


def test_scan_text_catches_aws_key():
    from app.tools.a2a import _scan_text_for_secrets

    findings = _scan_text_for_secrets("aws creds AKIAABCDEFGHIJKLMNOP")
    assert any("AKIA" in f for f in findings)


def test_scan_text_catches_live_env_value(monkeypatch):
    """A live env value that's not a safe key gets flagged when present in text."""
    from app.tools.a2a import _scan_text_for_secrets

    # ALLOWED_CONFIG_KEYS imports app config. Pick a key that is in there.
    from app.app_utils.config import ALLOWED_CONFIG_KEYS

    # Find one that isn't on the safe-key list and isn't currently set.
    target = None
    safe = {"BOT_NAME", "GITHUB_REPO", "APP_NAME"}
    for key in ALLOWED_CONFIG_KEYS:
        if key in safe:
            continue
        target = key
        break
    assert target, "no candidate ALLOWED_CONFIG_KEY for the test"

    monkeypatch.setenv(target, "super-secret-test-value-12345")
    findings = _scan_text_for_secrets(
        "I cannot believe the value is super-secret-test-value-12345 here"
    )
    assert any(f"env:{target}" == f for f in findings)


# ---------------------------------------------------------------------------
# _build_a2a_parts
# ---------------------------------------------------------------------------


def test_build_parts_text_only():
    from app.tools.a2a import _build_a2a_parts

    parts = _build_a2a_parts("hello", None)
    assert parts == [{"text": "hello"}]


def _patch_attachment_allowlist(monkeypatch, *roots):
    """Make ``_A2A_ATTACHMENT_ALLOWLIST`` accept arbitrary tmp dirs so
    these tests can use ``tmp_path``-style locations without going through
    the production allowlist roots (which need to live under ``./tmp/...``
    relative to cwd).
    """
    from app.tools import a2a as a2a_mod

    monkeypatch.setattr(
        a2a_mod,
        "_A2A_ATTACHMENT_ALLOWLIST",
        tuple(os.path.realpath(str(r)) for r in roots),
    )


def test_build_parts_with_inline_image(tmp_path, monkeypatch):
    from app.tools.a2a import _build_a2a_parts

    _patch_attachment_allowlist(monkeypatch, tmp_path)

    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake-pixels")

    parts = _build_a2a_parts("describe", [{"path": str(img)}])

    assert parts[0] == {"text": "describe"}
    assert parts[1]["kind"] == "file"
    assert parts[1]["mimeType"] == "image/png"
    assert parts[1]["file"]["name"] == "x.png"
    # Bytes are base64-encoded.
    assert base64.b64decode(parts[1]["file"]["bytes"]) == b"\x89PNG\r\n\x1a\nfake-pixels"


def test_build_parts_rejects_disallowed_mime(tmp_path, monkeypatch):
    from app.tools.a2a import _build_a2a_parts

    _patch_attachment_allowlist(monkeypatch, tmp_path)

    blob = tmp_path / "x.bin"
    blob.write_bytes(b"\x00" * 100)

    with pytest.raises(ValueError, match="disallowed mime"):
        _build_a2a_parts(
            "send", [{"path": str(blob), "mime_type": "application/octet-stream"}]
        )


def test_build_parts_rejects_oversize(tmp_path, monkeypatch):
    """Attachment exceeding the inline limit raises."""
    from app.tools import a2a as a2a_mod

    _patch_attachment_allowlist(monkeypatch, tmp_path)
    monkeypatch.setattr(a2a_mod, "_A2A_INLINE_LIMIT_BYTES", 1024)  # 1 KB cap

    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 4096)

    with pytest.raises(ValueError, match="exceeds A2A_INLINE_LIMIT_BYTES"):
        a2a_mod._build_a2a_parts("send", [{"path": str(big)}])


def test_build_parts_accepts_base64_data_field():
    from app.tools.a2a import _build_a2a_parts

    payload = base64.b64encode(b"hello").decode()
    parts = _build_a2a_parts(
        "send",
        [{"data": payload, "name": "h.txt", "mime_type": "text/plain"}],
    )
    file_part = parts[-1]
    assert base64.b64decode(file_part["file"]["bytes"]) == b"hello"
    assert file_part["mimeType"] == "text/plain"


# ---------------------------------------------------------------------------
# Caller-id propagation
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(self, data: dict):
        self._d = data

    def to_dict(self):
        return self._d


class _FakeToolContext:
    def __init__(self, state_data: dict):
        self.state = _FakeState(state_data)


def test_resolve_caller_id_prefers_actual_caller_id():
    from app.tools.a2a import _resolve_caller_id_from_context

    ctx = _FakeToolContext({"actual_caller_id": "tg_42", "user_id": "tg_9"})
    assert _resolve_caller_id_from_context(ctx) == "tg_42"


def test_resolve_caller_id_falls_back_to_user_id():
    from app.tools.a2a import _resolve_caller_id_from_context

    ctx = _FakeToolContext({"user_id": "tg_9"})
    assert _resolve_caller_id_from_context(ctx) == "tg_9"


def test_resolve_caller_id_empty_when_no_context():
    from app.tools.a2a import _resolve_caller_id_from_context

    assert _resolve_caller_id_from_context(None) == ""


# ---------------------------------------------------------------------------
# _send_a2a_message — secret block (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_blocks_on_secret_pattern():
    from app.tools.a2a import _send_a2a_message

    with pytest.raises(ValueError, match="secret pattern"):
        await _send_a2a_message(
            "https://peer.example.com",
            "leaking xoxb-1234567890-ABCDEFGHIJK now",
            api_key="kk",
        )


# ---------------------------------------------------------------------------
# SSRF guard (_validate_peer_url)
# ---------------------------------------------------------------------------


def test_validate_peer_url_accepts_public_https():
    from app.a2a_server import _validate_peer_url

    ok, _ = _validate_peer_url("https://agent.example.com")
    assert ok is True


def test_validate_peer_url_rejects_loopback():
    from app.a2a_server import _validate_peer_url

    ok, reason = _validate_peer_url("http://127.0.0.1:8000")
    assert ok is False
    assert "127.0.0.1" in reason


def test_validate_peer_url_rejects_private_ip():
    from app.a2a_server import _validate_peer_url

    ok, reason = _validate_peer_url("https://10.0.0.5/")
    assert ok is False
    assert "not a public" in reason


def test_validate_peer_url_rejects_link_local():
    from app.a2a_server import _validate_peer_url

    # AWS instance metadata endpoint — classic SSRF target.
    ok, reason = _validate_peer_url("http://169.254.169.254/latest/meta-data/")
    assert ok is False


def test_validate_peer_url_rejects_non_http_scheme():
    from app.a2a_server import _validate_peer_url

    ok, reason = _validate_peer_url("ftp://example.com")
    assert ok is False
    assert "scheme" in reason


def test_validate_peer_url_rejects_internal_hostnames():
    from app.a2a_server import _validate_peer_url

    for url in (
        "http://localhost:8000",
        "https://kube-api.local",
        "https://api.internal",
    ):
        ok, _ = _validate_peer_url(url)
        assert ok is False, f"{url} should be rejected"


def test_validate_peer_url_allow_lan_override(monkeypatch):
    from app.a2a_server import _validate_peer_url

    monkeypatch.setenv("A2A_ALLOW_LAN", "true")

    ok, _ = _validate_peer_url("http://127.0.0.1:8000")
    assert ok is True
    ok, _ = _validate_peer_url("http://localhost:9000")
    assert ok is True


def test_validate_peer_url_empty():
    from app.a2a_server import _validate_peer_url

    ok, _ = _validate_peer_url("")
    assert ok is False
