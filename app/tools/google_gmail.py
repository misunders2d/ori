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
