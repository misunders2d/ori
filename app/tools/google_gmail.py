"""Gmail read-only tools — per-user OAuth2 access.

Uses the same token store and auth helpers as Drive/Sheets/Calendar.
Six tools: list/get messages, list/get threads, list labels, download attachment.
Write tools (send/modify/drafts) can be added alongside without refactoring.
"""

import asyncio
import base64
import logging
import os
import time
from html.parser import HTMLParser

import httpx
from google.adk.tools.tool_context import ToolContext

from app.tools.google_drive import _auth_headers, _get_user_email, _get_valid_token
from app.tools.google_oauth.token_store import get_token

logger = logging.getLogger(__name__)

_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

_ATTACHMENT_DIR = "./tmp/gmail_attachments"


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
    """Decode a Gmail base64url body payload to text."""
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")


def _find_part(payload: dict, mime: str) -> dict | None:
    """DFS through payload.parts looking for the first part with the given mimeType."""
    if payload.get("mimeType") == mime:
        return payload
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime)
        if found is not None:
            return found
    return None


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
