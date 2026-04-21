# Gmail Read-Only Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-user Gmail read-only access as six tools exposed through the existing Google Workspace toolset, reusing the current OAuth device flow.

**Architecture:** One new module `app/tools/google_gmail.py` mirroring `app/tools/google_calendar.py` — reuses `_get_user_email` / `_get_valid_token` / `_auth_headers` from `app/tools/google_drive.py`. Six tools. Three pure helpers (`_decode_body`, `_truncate`, `_sweep_attachments`) built TDD-first. One scope added to the shared device-flow scope list. Toolset registration and skill doc updated.

**Tech Stack:** Python 3, `httpx` async, `pytest` + `pytest-asyncio`, `unittest.mock` (`patch`, `AsyncMock`, `MagicMock`). Gmail API v1 (REST). stdlib `html.parser` for HTML-to-text fallback. stdlib `base64.urlsafe_b64decode` for body/attachment decoding.

**Spec:** `docs/superpowers/specs/2026-04-21-gmail-read-only-support-design.md`

---

## File Structure

**New files:**
- `app/tools/google_gmail.py` — helpers + six tools
- `tests/test_google_gmail.py` — tests for helpers and tool surface

**Modified files:**
- `app/tools/google_oauth/device_flow.py` — add `gmail.readonly` to `SCOPES`
- `app/toolsets/google_workspace.py` — register six new tools
- `skills/google-workspace-skill/SKILL.md` — update description, add Gmail section, extend gotchas

**Runtime-created:**
- `./tmp/gmail_attachments/` — attachment cache, swept on write

---

## Task 1: Add Gmail scope to OAuth device flow

**Files:**
- Modify: `app/tools/google_oauth/device_flow.py:19-24`
- Test: `tests/test_google_gmail.py` (new)

- [ ] **Step 1: Create the test file and write the failing test**

Create `tests/test_google_gmail.py`:

```python
"""Tests for Gmail read-only tools — helpers and tool surface."""

from app.tools.google_oauth.device_flow import SCOPES


def test_gmail_readonly_scope_present():
    assert "https://www.googleapis.com/auth/gmail.readonly" in SCOPES
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_google_gmail.py::test_gmail_readonly_scope_present -v`
Expected: FAIL — `AssertionError` (scope not yet in SCOPES).

- [ ] **Step 3: Add the scope**

In `app/tools/google_oauth/device_flow.py:19-24`, change:

```python
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
]
```

to:

```python
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_google_gmail.py::test_gmail_readonly_scope_present -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/tools/google_oauth/device_flow.py tests/test_google_gmail.py
git commit -m "feat(oauth): add gmail.readonly scope to device flow"
```

---

## Task 2: Create `google_gmail.py` module skeleton + `_decode_body` helper

Gmail's `messages.get` API returns a tree-structured payload where the actual body lives in base64url-encoded leaf parts. `_decode_body` walks this tree, preferring `text/plain` over `text/html`, and strips HTML via stdlib as a fallback.

**Files:**
- Create: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
import base64

from app.tools.google_gmail import _decode_body


def _b64url(s: str) -> str:
    """Encode a string the way Gmail does — urlsafe base64, no padding stripped."""
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k decode_body`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.tools.google_gmail'`.

- [ ] **Step 3: Create the module with `_decode_body`**

Create `app/tools/google_gmail.py`:

```python
"""Gmail read-only tools — per-user OAuth2 access.

Uses the same token store and auth helpers as Drive/Sheets/Calendar.
Six tools: list/get messages, list/get threads, list labels, download attachment.
Write tools (send/modify/drafts) can be added alongside without refactoring.
"""

import base64
import logging
import os
import time
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

_ATTACHMENT_DIR = "./tmp/gmail_attachments"


# ---------------------------------------------------------------------------
# Helpers — pure, no I/O
# ---------------------------------------------------------------------------


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return " ".join("".join(self._chunks).split())


def _strip_html(html: str) -> str:
    parser = _HTMLStripper()
    parser.feed(html)
    return parser.text()


def _decode_part_data(data: str) -> str:
    """Decode a Gmail base64url body payload to text.

    Gmail uses urlsafe base64 and may omit padding; add it back before decoding.
    """
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")


def _decode_body(payload: dict) -> str:
    """Walk a Gmail message payload tree and return the best-effort plain text body.

    Preference order: text/plain anywhere in the tree, then text/html (stripped).
    Returns empty string if neither mime type is present.
    """
    plain = _find_part(payload, "text/plain")
    if plain is not None:
        return _decode_part_data(plain.get("body", {}).get("data", ""))

    html = _find_part(payload, "text/html")
    if html is not None:
        raw = _decode_part_data(html.get("body", {}).get("data", ""))
        return _strip_html(raw)

    return ""


def _find_part(payload: dict, mime: str) -> dict | None:
    """DFS through payload.parts looking for the first part with the given mimeType."""
    if payload.get("mimeType") == mime:
        return payload
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime)
        if found is not None:
            return found
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k decode_body`
Expected: PASS for all five `decode_body` tests.

- [ ] **Step 5: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): _decode_body helper with text/plain preference and HTML fallback"
```

---

## Task 3: `_truncate` helper

Truncation protects agent context from long newsletters and supplier threads. Default clip at 8000 chars; `full=True` returns untruncated body.

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
from app.tools.google_gmail import _truncate


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k truncate`
Expected: FAIL — `ImportError: cannot import name '_truncate'`.

- [ ] **Step 3: Add `_truncate` to `google_gmail.py`**

Append after `_find_part` in `app/tools/google_gmail.py`:

```python
_TRUNCATE_DEFAULT = 8000


def _truncate(body: str, full: bool) -> str:
    """Clip body to _TRUNCATE_DEFAULT chars unless full=True.

    When clipped, appends a suffix noting how many chars were dropped and how to
    retrieve the full body, so the agent can choose whether to re-fetch.
    """
    if full or len(body) <= _TRUNCATE_DEFAULT:
        return body
    dropped = len(body) - _TRUNCATE_DEFAULT
    return body[:_TRUNCATE_DEFAULT] + f"...[truncated, {dropped} more chars — call with full=True]"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k truncate`
Expected: PASS for all four `truncate` tests.

- [ ] **Step 5: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): _truncate helper with full=True override"
```

---

## Task 4: `_sweep_attachments` helper

Purges attachments older than `GMAIL_ATTACHMENT_TTL_HOURS` (default 24) from `./tmp/gmail_attachments/`. Called at the start of every `gmail_download_attachment` call. No background job.

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
import os
import time as time_mod

from unittest.mock import patch

from app.tools.google_gmail import _sweep_attachments


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
    # Backdate the "old" file by 48h
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
    # File is 2h old
    two_h_ago = time_mod.time() - 2 * 3600
    os.utime(f, (two_h_ago, two_h_ago))
    with patch("app.tools.google_gmail._ATTACHMENT_DIR", str(d)), \
         patch.dict(os.environ, {"GMAIL_ATTACHMENT_TTL_HOURS": "1"}):
        _sweep_attachments()
    assert not f.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k sweep`
Expected: FAIL — `ImportError: cannot import name '_sweep_attachments'`.

- [ ] **Step 3: Add `_sweep_attachments` to `google_gmail.py`**

Append after `_truncate` in `app/tools/google_gmail.py`:

```python
def _sweep_attachments() -> None:
    """Remove expired files from the attachment cache. Missing dir is a no-op.

    TTL hours read from GMAIL_ATTACHMENT_TTL_HOURS env var, default 24.
    """
    if not os.path.isdir(_ATTACHMENT_DIR):
        return
    try:
        ttl_hours = float(os.environ.get("GMAIL_ATTACHMENT_TTL_HOURS", "24"))
    except ValueError:
        ttl_hours = 24.0
    cutoff = time.time() - (ttl_hours * 3600)
    for entry in os.scandir(_ATTACHMENT_DIR):
        if not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
        except OSError as e:
            logger.warning("Failed to sweep %s: %s", entry.path, e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k sweep`
Expected: PASS for all three `sweep` tests.

- [ ] **Step 5: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): _sweep_attachments mtime-based TTL cache purge"
```

---

## Task 5: `gmail_list_labels` tool

Simplest full tool — single API call, minimal transformation. Establishes the pattern for the remaining tools (not-connected guard → token → httpx call → shape the response).

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

First, add a shared httpx mock helper near the top of `tests/test_google_gmail.py` (below the existing imports):

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_response(json_data: dict, status: int = 200):
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    return resp


def _mock_async_client(get_responses=None, post_responses=None):
    """Build a mock httpx.AsyncClient that yields the given responses from get/post.

    Returns the mock client class — patch `httpx.AsyncClient` with it.
    """
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
```

Append the label tests:

```python
@pytest.mark.asyncio
async def test_gmail_list_labels_not_connected():
    from app.tools.google_gmail import gmail_list_labels
    with patch("app.tools.google_gmail.get_token", return_value=None):
        result = await gmail_list_labels(tool_context=_mock_ctx())
    assert result["status"] == "error"
    assert "not connected" in result["message"].lower()


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k list_labels`
Expected: FAIL — `ImportError: cannot import name 'gmail_list_labels'`.

- [ ] **Step 3: Add the tool**

Append to `app/tools/google_gmail.py` — first add the remaining imports at the top (alongside existing imports):

```python
import httpx
from google.adk.tools.tool_context import ToolContext

from app.tools.google_drive import _get_user_email, _get_valid_token, _auth_headers
from app.tools.google_oauth.token_store import get_token
```

Then append the tool function at the bottom:

```python
# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def gmail_list_labels(tool_context: ToolContext = None) -> dict:
    """List all Gmail labels for the current user.

    Returns:
        dict with `labels`: list of {id, name, type} where type is 'system' or 'user'.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Gmail not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_GMAIL_API}/labels", headers=_auth_headers(token))
            resp.raise_for_status()
            data = resp.json()
        labels = [
            {"id": l["id"], "name": l.get("name", ""), "type": l.get("type", "user")}
            for l in data.get("labels", [])
        ]
        return {"status": "success", "count": len(labels), "labels": labels}
    except Exception as e:
        return {"status": "error", "message": f"Gmail API error: {e}"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k list_labels`
Expected: PASS for both `list_labels` tests.

- [ ] **Step 5: Run the full test file to make sure nothing regressed**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): gmail_list_labels tool"
```

---

## Task 6: `gmail_get_message` tool

Fetches a single message with full payload, decodes the body, truncates per flag, and extracts attachment metadata. Core of the whole feature.

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
def _sample_message(body_text: str, attachments: list[dict] | None = None) -> dict:
    """Build a Gmail messages.get response payload for tests."""
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k get_message`
Expected: FAIL — `ImportError: cannot import name 'gmail_get_message'`.

- [ ] **Step 3: Add the tool + header/attachment helpers**

Append to `app/tools/google_gmail.py` (above the tool block if you prefer grouping, or below `gmail_list_labels`):

```python
def _headers_to_dict(headers: list[dict]) -> dict:
    """Flatten Gmail's list-of-{name,value} headers into a lowercased dict."""
    out: dict[str, str] = {}
    for h in headers or []:
        name = h.get("name", "").lower()
        if name:
            out[name] = h.get("value", "")
    return out


def _extract_attachments(payload: dict) -> list[dict]:
    """Walk the payload tree and return metadata for every part with an attachmentId."""
    out: list[dict] = []

    def walk(node: dict) -> None:
        body = node.get("body", {}) or {}
        aid = body.get("attachmentId")
        if aid:
            out.append({
                "id": aid,
                "filename": node.get("filename", ""),
                "mime": node.get("mimeType", "application/octet-stream"),
                "size": body.get("size", 0),
            })
        for part in node.get("parts", []) or []:
            walk(part)

    walk(payload)
    return out


async def gmail_get_message(
    message_id: str,
    full: bool = False,
    tool_context: ToolContext = None,
) -> dict:
    """Fetch a single Gmail message with headers, body, and attachment metadata.

    Args:
        message_id: Gmail message ID (from gmail_list_messages).
        full: If True, return the full decoded body. If False (default), truncate to 8000 chars.

    Returns:
        dict with id, thread_id, headers (lowercased), body, attachments.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Gmail not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GMAIL_API}/messages/{message_id}",
                params={"format": "full"},
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
        payload = data.get("payload", {})
        body = _truncate(_decode_body(payload), full=full)
        return {
            "status": "success",
            "id": data.get("id", message_id),
            "thread_id": data.get("threadId", ""),
            "headers": _headers_to_dict(payload.get("headers", [])),
            "body": body,
            "attachments": _extract_attachments(payload),
        }
    except Exception as e:
        return {"status": "error", "message": f"Gmail API error: {e}"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k get_message`
Expected: PASS for all four `get_message` tests.

- [ ] **Step 5: Run the full file to verify no regressions**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): gmail_get_message with body truncation and attachment metadata"
```

---

## Task 7: `gmail_list_messages` tool

Gmail's `messages.list` returns only `{id, threadId}`, so rich listing requires an N+1 enrichment pattern — one `list` + up to `max_results` parallel `get?format=metadata` calls. `asyncio.gather` keeps latency at roughly one round-trip.

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
def _metadata_message(id_: str, subject: str, from_: str, has_attachment: bool = False) -> dict:
    """Build a Gmail messages.get?format=metadata response shape."""
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
    # Order must match list response order (asyncio.gather preserves input order)
    assert [m["id"] for m in result["messages"]] == ["m1", "m2"]
    assert result["messages"][0]["from"] == "amazon@amazon.com"
    assert result["messages"][0]["subject"] == "Order update"
    assert result["messages"][0]["has_attachments"] is True
    assert result["messages"][0]["thread_id"] == "thr_m1"
    assert result["messages"][1]["has_attachments"] is False


@pytest.mark.asyncio
async def test_gmail_list_messages_empty_result():
    from app.tools.google_gmail import gmail_list_messages
    list_resp = _mock_response({})  # No "messages" key when zero results
    client_cls, _ = _mock_async_client(get_responses=[list_resp])
    with patch("app.tools.google_gmail._get_valid_token", AsyncMock(return_value="tok")), \
         patch("app.tools.google_gmail._get_user_email", return_value="user@test.com"), \
         patch("app.tools.google_gmail.httpx.AsyncClient", client_cls):
        result = await gmail_list_messages(tool_context=_mock_ctx())
    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["messages"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k list_messages`
Expected: FAIL — `ImportError: cannot import name 'gmail_list_messages'`.

- [ ] **Step 3: Add the tool**

First, add `import asyncio` at the top of `app/tools/google_gmail.py` (if not already present).

Then append:

```python
def _has_attachments(payload: dict) -> bool:
    def walk(node: dict) -> bool:
        body = node.get("body", {}) or {}
        if body.get("attachmentId"):
            return True
        for p in node.get("parts", []) or []:
            if walk(p):
                return True
        return False
    return walk(payload)


async def _fetch_message_metadata(client: httpx.AsyncClient, token: str, message_id: str) -> dict:
    """Fetch a single message with format=metadata and shape it for list output."""
    resp = await client.get(
        f"{_GMAIL_API}/messages/{message_id}",
        params={"format": "metadata"},
        headers=_auth_headers(token),
    )
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload", {})
    headers = _headers_to_dict(payload.get("headers", []))
    return {
        "id": data.get("id", message_id),
        "thread_id": data.get("threadId", ""),
        "snippet": data.get("snippet", ""),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "has_attachments": _has_attachments(payload),
    }


async def gmail_list_messages(
    query: str = "",
    max_results: int = 25,
    label_ids: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """List Gmail messages matching a query, enriched with headers and snippet.

    Args:
        query: Gmail search syntax — e.g. 'from:amazon.com is:unread newer_than:7d'.
        max_results: Max messages to return (default 25, capped at 100).
        label_ids: Comma-separated label IDs to filter by (from gmail_list_labels).

    Returns:
        dict with `messages`: list of {id, thread_id, snippet, from, subject, date, has_attachments}.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Gmail not connected for {email}. Use google_connect first."}

    params: dict = {"maxResults": min(max(max_results, 1), 100)}
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = [lid.strip() for lid in label_ids.split(",") if lid.strip()]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GMAIL_API}/messages",
                params=params,
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            ids = [m["id"] for m in resp.json().get("messages", []) or []]
            if not ids:
                return {"status": "success", "count": 0, "messages": []}
            enriched = await asyncio.gather(
                *(_fetch_message_metadata(client, token, mid) for mid in ids)
            )
        return {"status": "success", "count": len(enriched), "messages": list(enriched)}
    except Exception as e:
        return {"status": "error", "message": f"Gmail API error: {e}"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k list_messages`
Expected: PASS for all three `list_messages` tests.

- [ ] **Step 5: Run the full file**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): gmail_list_messages with parallel metadata enrichment"
```

---

## Task 8: `gmail_get_thread` tool

Fetches a full thread and returns each message through the same decode/truncate pipeline as `gmail_get_message`. Reuses helpers — no new decoding logic.

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k get_thread`
Expected: FAIL — `ImportError: cannot import name 'gmail_get_thread'`.

- [ ] **Step 3: Add the tool + a shared message shaper**

Append to `app/tools/google_gmail.py`:

```python
def _shape_message_full(data: dict, full: bool) -> dict:
    """Shape a messages.get?format=full response into the public tool dict."""
    payload = data.get("payload", {})
    return {
        "id": data.get("id", ""),
        "thread_id": data.get("threadId", ""),
        "headers": _headers_to_dict(payload.get("headers", [])),
        "body": _truncate(_decode_body(payload), full=full),
        "attachments": _extract_attachments(payload),
    }


async def gmail_get_thread(
    thread_id: str,
    full: bool = False,
    tool_context: ToolContext = None,
) -> dict:
    """Fetch a full Gmail thread — all messages in order.

    Args:
        thread_id: Gmail thread ID (from gmail_list_threads or gmail_list_messages).
        full: If True, return full bodies. If False (default), truncate each to 8000 chars.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Gmail not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GMAIL_API}/threads/{thread_id}",
                params={"format": "full"},
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
        messages = [_shape_message_full(m, full=full) for m in data.get("messages", [])]
        return {
            "status": "success",
            "id": data.get("id", thread_id),
            "count": len(messages),
            "messages": messages,
        }
    except Exception as e:
        return {"status": "error", "message": f"Gmail API error: {e}"}
```

Note: `gmail_get_message` can now be simplified to reuse `_shape_message_full` — but do NOT refactor it in this task. Leave Task 6's implementation as-is to keep this task isolated; a follow-up cleanup task would be its own PR.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k get_thread`
Expected: PASS for all three `get_thread` tests.

- [ ] **Step 5: Run the full file**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): gmail_get_thread with per-message truncation"
```

---

## Task 9: `gmail_list_threads` tool

Same N+1 pattern as `gmail_list_messages`, but enrichment derives `message_count`, `participants`, and `last_date` from a per-thread `threads.get?format=metadata` call. `participants` = unique `From` addresses across all messages in the thread.

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
def _thread_metadata(id_: str, froms: list[str], last_date: str) -> dict:
    """Build a threads.get?format=metadata response for tests.

    Gmail's metadata format returns each message with its payload.headers only.
    """
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k list_threads`
Expected: FAIL — `ImportError: cannot import name 'gmail_list_threads'`.

- [ ] **Step 3: Add the tool**

Append to `app/tools/google_gmail.py`:

```python
async def _fetch_thread_metadata(client: httpx.AsyncClient, token: str, thread_id: str) -> dict:
    """Fetch thread metadata and summarize it for list output."""
    resp = await client.get(
        f"{_GMAIL_API}/threads/{thread_id}",
        params={"format": "metadata"},
        headers=_auth_headers(token),
    )
    resp.raise_for_status()
    data = resp.json()
    messages = data.get("messages", []) or []
    participants: list[str] = []
    last_date = ""
    for m in messages:
        headers = _headers_to_dict(m.get("payload", {}).get("headers", []))
        sender = headers.get("from", "")
        if sender and sender not in participants:
            participants.append(sender)
        d = headers.get("date", "")
        if d:
            last_date = d  # Gmail returns messages oldest-first; the last one wins
    return {
        "id": data.get("id", thread_id),
        "snippet": data.get("snippet", ""),
        "message_count": len(messages),
        "participants": participants,
        "last_date": last_date,
    }


async def gmail_list_threads(
    query: str = "",
    max_results: int = 25,
    tool_context: ToolContext = None,
) -> dict:
    """List Gmail threads matching a query, enriched with participants and last date.

    Args:
        query: Gmail search syntax.
        max_results: Max threads to return (default 25, capped at 100).

    Returns:
        dict with `threads`: list of {id, snippet, message_count, participants, last_date}.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Gmail not connected for {email}. Use google_connect first."}

    params: dict = {"maxResults": min(max(max_results, 1), 100)}
    if query:
        params["q"] = query

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_GMAIL_API}/threads",
                params=params,
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            ids = [t["id"] for t in resp.json().get("threads", []) or []]
            if not ids:
                return {"status": "success", "count": 0, "threads": []}
            enriched = await asyncio.gather(
                *(_fetch_thread_metadata(client, token, tid) for tid in ids)
            )
        return {"status": "success", "count": len(enriched), "threads": list(enriched)}
    except Exception as e:
        return {"status": "error", "message": f"Gmail API error: {e}"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k list_threads`
Expected: PASS for all three `list_threads` tests.

- [ ] **Step 5: Run the full file**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): gmail_list_threads with parallel enrichment"
```

---

## Task 10: `gmail_download_attachment` tool

Fetches an attachment by ID, decodes base64url, writes to `./tmp/gmail_attachments/`, and invokes `_sweep_attachments` before the write so the cache stays bounded.

**Files:**
- Modify: `app/tools/google_gmail.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_google_gmail.py`:

```python
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
    # When caller provides no filename, fall back to the attachment ID
    assert result["filename"] == "att_1"
    assert result["file_path"] == os.path.join(attach_dir, "msg_1_att_1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_google_gmail.py -v -k download_attachment`
Expected: FAIL — `ImportError: cannot import name 'gmail_download_attachment'`.

- [ ] **Step 3: Add the tool**

Append to `app/tools/google_gmail.py`:

```python
def _safe_filename(name: str) -> str:
    """Strip path separators and keep the basename only."""
    return os.path.basename(name).replace("/", "_").replace("\\", "_") or "file"


async def gmail_download_attachment(
    message_id: str,
    attachment_id: str,
    filename: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Download a Gmail attachment to the local cache.

    Saves to ./tmp/gmail_attachments/{message_id}_{filename}. The cache is
    swept at the start of this call — files older than GMAIL_ATTACHMENT_TTL_HOURS
    (default 24) are removed.

    Args:
        message_id: Gmail message ID the attachment belongs to.
        attachment_id: Attachment ID (from gmail_get_message).
        filename: Optional original filename. Falls back to attachment_id if empty.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Gmail not connected for {email}. Use google_connect first."}

    _sweep_attachments()
    os.makedirs(_ATTACHMENT_DIR, exist_ok=True)

    safe_name = _safe_filename(filename or attachment_id)
    out_path = os.path.join(_ATTACHMENT_DIR, f"{message_id}_{safe_name}")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"{_GMAIL_API}/messages/{message_id}/attachments/{attachment_id}",
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
        raw_b64 = data.get("data", "")
        if not raw_b64:
            return {"status": "error", "message": "Attachment payload was empty."}
        padded = raw_b64 + "=" * (-len(raw_b64) % 4)
        content = base64.urlsafe_b64decode(padded.encode())
        with open(out_path, "wb") as f:
            f.write(content)
        return {
            "status": "success",
            "file_path": out_path,
            "filename": safe_name,
            "size_bytes": len(content),
        }
    except Exception as e:
        return {"status": "error", "message": f"Gmail API error: {e}"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_google_gmail.py -v -k download_attachment`
Expected: PASS for all three `download_attachment` tests.

- [ ] **Step 5: Run the full file**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/tools/google_gmail.py tests/test_google_gmail.py
git commit -m "feat(gmail): gmail_download_attachment with TTL-swept local cache"
```

---

## Task 11: Register Gmail tools in the Google Workspace toolset

**Files:**
- Modify: `app/toolsets/google_workspace.py`
- Modify: `tests/test_google_gmail.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_google_gmail.py`:

```python
@pytest.mark.asyncio
async def test_google_workspace_toolset_registers_gmail_tools():
    from app.toolsets.google_workspace import GoogleWorkspaceToolset
    toolset = GoogleWorkspaceToolset()
    tools = await toolset.get_tools()
    names = {t.func.__name__ for t in tools}
    assert "gmail_list_messages" in names
    assert "gmail_get_message" in names
    assert "gmail_list_threads" in names
    assert "gmail_get_thread" in names
    assert "gmail_list_labels" in names
    assert "gmail_download_attachment" in names
    # Existing tools should still be there too
    assert "drive_list_files" in names
    assert "calendar_list_events" in names
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_google_gmail.py -v -k toolset_registers_gmail`
Expected: FAIL — assertion on `gmail_list_messages` not in names.

- [ ] **Step 3: Register the tools**

Replace the entirety of `app/toolsets/google_workspace.py` with:

```python
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class GoogleWorkspaceToolset(BaseToolset):
    """Google Drive, Sheets, Calendar, and Gmail tools with per-user OAuth2."""

    async def get_tools(self, readonly_context=None):
        from app.tools.google_drive import (
            google_connect,
            google_connect_complete,
            google_disconnect,
            drive_list_files,
            drive_download_file,
            sheets_read,
            sheets_write,
            sheets_create,
        )
        from app.tools.google_calendar import (
            calendar_list,
            calendar_list_events,
            calendar_create_event,
            calendar_update_event,
            calendar_delete_event,
        )
        from app.tools.google_gmail import (
            gmail_list_messages,
            gmail_get_message,
            gmail_list_threads,
            gmail_get_thread,
            gmail_list_labels,
            gmail_download_attachment,
        )

        return [
            FunctionTool(func=google_connect),
            FunctionTool(func=google_connect_complete),
            FunctionTool(func=google_disconnect),
            FunctionTool(func=drive_list_files),
            FunctionTool(func=drive_download_file),
            FunctionTool(func=sheets_read),
            FunctionTool(func=sheets_write),
            FunctionTool(func=sheets_create),
            FunctionTool(func=calendar_list),
            FunctionTool(func=calendar_list_events),
            FunctionTool(func=calendar_create_event),
            FunctionTool(func=calendar_update_event),
            FunctionTool(func=calendar_delete_event),
            FunctionTool(func=gmail_list_messages),
            FunctionTool(func=gmail_get_message),
            FunctionTool(func=gmail_list_threads),
            FunctionTool(func=gmail_get_thread),
            FunctionTool(func=gmail_list_labels),
            FunctionTool(func=gmail_download_attachment),
        ]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_google_gmail.py -v -k toolset_registers_gmail`
Expected: PASS.

- [ ] **Step 5: Run the full file**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add app/toolsets/google_workspace.py tests/test_google_gmail.py
git commit -m "feat(gmail): register Gmail tools in GoogleWorkspaceToolset"
```

---

## Task 12: Update the Google Workspace skill doc

Skill doc update — no unit tests. Verification is a manual read-through + checking that the skill frontmatter mentions Gmail (because the coordinator agent selects skills by description).

**Files:**
- Modify: `skills/google-workspace-skill/SKILL.md`

- [ ] **Step 1: Update the frontmatter**

In `skills/google-workspace-skill/SKILL.md:1-4`, change:

```markdown
---
name: google-workspace-skill
description: "Google Drive, Sheets, and Calendar integration — per-user OAuth2 connection, file management, spreadsheet operations, calendar management."
---
```

to:

```markdown
---
name: google-workspace-skill
description: "Google Drive, Sheets, Calendar, and Gmail integration — per-user OAuth2 connection, file management, spreadsheet operations, calendar management, read-only Gmail access."
---
```

- [ ] **Step 2: Update the intro paragraph**

In `skills/google-workspace-skill/SKILL.md:7-8`, change:

```markdown
# Google Workspace Skill

Per-user Google Drive, Sheets, and Calendar access via OAuth2 device code flow. Each user connects their own Google account — they see their own files and calendars.
```

to:

```markdown
# Google Workspace Skill

Per-user Google Drive, Sheets, Calendar, and Gmail access via OAuth2 device code flow. Each user connects their own Google account — they see their own files, calendars, and mailbox. Gmail is read-only for now.
```

- [ ] **Step 3: Update the "One connection grants access…" line**

In `skills/google-workspace-skill/SKILL.md:18`, change:

```markdown
One connection grants access to Drive, Sheets, AND Calendar. No need to connect separately.
```

to:

```markdown
One connection grants access to Drive, Sheets, Calendar, AND Gmail. No need to connect separately.
```

- [ ] **Step 4: Add a Gmail tools section**

After the Calendar tools table (around line 49, after `| calendar_delete_event | Delete an event |`), add:

```markdown

### Gmail (read-only)
| Tool | Purpose |
|------|---------|
| `gmail_list_labels()` | Enumerate labels. Returns system (INBOX, SENT, etc.) and user-defined labels. |
| `gmail_list_messages(query, max_results, label_ids)` | Search messages. Uses Gmail search syntax. |
| `gmail_get_message(message_id, full)` | Full headers + body + attachment metadata. Body truncated to 8000 chars unless `full=True`. |
| `gmail_list_threads(query, max_results)` | Thread-centric list — useful for back-and-forth conversations. |
| `gmail_get_thread(thread_id, full)` | All messages in a thread. Each body truncated per `full`. |
| `gmail_download_attachment(message_id, attachment_id, filename)` | Save an attachment to `./tmp/gmail_attachments/`. Cached 24h by default. |
```

- [ ] **Step 5: Add a Gmail Usage section**

After the `## Calendar Usage` section (around line 66, before `## NOT Your Job: Personal Reminders`), add:

```markdown

## Gmail Usage

- **Search syntax:** Use Gmail's native search operators — `from:`, `to:`, `subject:`, `is:unread`, `has:attachment`, `newer_than:7d`, `label:INBOX`. Combine freely: `from:amazon.com is:unread newer_than:3d`.
- **Body truncation:** By default, bodies are clipped to 8000 chars and end with a `[truncated, N more chars — call with full=True]` marker. Call the tool again with `full=True` when you genuinely need the whole body.
- **Attachments:** `gmail_download_attachment` writes to `./tmp/gmail_attachments/{message_id}_{filename}`. Files older than 24h are purged automatically on the next download (override with `GMAIL_ATTACHMENT_TTL_HOURS`). The cache is not a Drive upload — it's a local scratch area.
- **Threads vs messages:** Prefer `gmail_list_threads` + `gmail_get_thread` for buyer↔seller or supplier conversations; use messages tools for one-shot notifications (Amazon alerts, receipts).
- **Read-only:** Sending, drafting, labeling, and archiving aren't available yet. If the user asks to send a reply, tell them this is a read-only integration for now.
```

- [ ] **Step 6: Update the "Existing users must reconnect" gotcha**

In the Gotchas section (around line 78), change:

```markdown
- **Existing users must reconnect** after Calendar was added (new scope). If Calendar tools fail with permission errors, tell the user to run `google_connect` again.
```

to:

```markdown
- **Existing users must reconnect** after Calendar or Gmail were added (new scopes). If Calendar or Gmail tools fail with permission errors, tell the user to run `google_connect` again.
```

- [ ] **Step 7: Add a Gmail API enablement gotcha**

At the end of the Gotchas section (after the existing Calendar API note), add:

```markdown
- **Gmail API must be enabled** in the Google Cloud project (console.cloud.google.com → APIs & Services → Gmail API).
```

- [ ] **Step 8: Verify the file reads cleanly**

Run: `cat skills/google-workspace-skill/SKILL.md | head -120`
Manually confirm: the frontmatter mentions Gmail, the tools section has six Gmail rows, the Gmail Usage section is present, and the gotchas mention reconnect + API enablement.

- [ ] **Step 9: Commit**

```bash
git add skills/google-workspace-skill/SKILL.md
git commit -m "docs(skill): document Gmail read-only tools in google-workspace-skill"
```

---

## Task 13: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full test suite**

Run: `pytest tests/test_google_gmail.py -v`
Expected: All tests PASS.

- [ ] **Step 2: Make sure nothing else broke**

Run: `pytest tests/test_google_oauth.py tests/test_oauth_service.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Lint / import check**

Run: `python -c "from app.tools.google_gmail import gmail_list_messages, gmail_get_message, gmail_list_threads, gmail_get_thread, gmail_list_labels, gmail_download_attachment; print('imports ok')"`
Expected: prints `imports ok`.

- [ ] **Step 4: Toolset import check**

Run: `python -c "import asyncio; from app.toolsets.google_workspace import GoogleWorkspaceToolset; tools = asyncio.run(GoogleWorkspaceToolset().get_tools()); print(f'{len(tools)} tools registered')"`
Expected: prints `19 tools registered` (13 existing + 6 new).

- [ ] **Step 5: Confirm the scope change requires reconnect**

Manual check only — the plan does not automate this because it affects live users:
- Existing users will see "insufficient scope" errors on any Gmail tool until they rerun `google_connect`.
- The skill doc now says this.
- No code change needed here.

---

## Out of Scope (future work)

Deferred per spec — not part of this plan:

- Write tools: `gmail_send_message`, `gmail_reply`, `gmail_modify_labels`, `gmail_create_draft`, `gmail_send_draft`.
- Additional scopes: `gmail.send`, `gmail.modify`.
- Push notifications / Gmail watch API.
- Retrofitting Drive's `drive_download_file` to also use TTL-based cleanup.
- Refactor: collapsing `gmail_get_message`'s body to reuse `_shape_message_full` (Task 8 added the shared shaper but intentionally did not refactor Task 6's implementation).
