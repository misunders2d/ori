"""Gmail read-only tools — per-user OAuth2 access.

Uses the same token store and auth helpers as Drive/Sheets/Calendar.
Six tools: list/get messages, list/get threads, list labels, download attachment.
Write tools (send/modify/drafts) can be added alongside without refactoring.
"""

import asyncio
import base64
import logging
import mimetypes
import os
import re
from html.parser import HTMLParser

import httpx
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.tools.google_drive import _auth_headers, _get_user_email, _get_valid_token
from app.util.file_convert import is_convertible, to_text
from app.util.tmp_sweeper import sweep_tmp

# Gemini inline-data acceptance: any image/*, audio/*, video/*, plus application/pdf.
# (Mirrors google.adk.tools.load_artifacts_tool._is_inline_mime_type_supported.)
_ARTIFACT_INLINE_PREFIXES = ("image/", "audio/", "video/")
_ARTIFACT_INLINE_EXACT = {"application/pdf"}


def _is_artifact_inline(mime: str) -> bool:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    return mime.startswith(_ARTIFACT_INLINE_PREFIXES) or mime in _ARTIFACT_INLINE_EXACT

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


# Matches Drive/Docs URLs that expose a file ID, plus the legacy ?id= form.
# Drive folder URLs are intentionally excluded — drive_download_file can't pull folders.
_DRIVE_URL_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/"
    r"(?:(?P<kind>file|document|spreadsheets|presentation)/d/(?P<id>[A-Za-z0-9_-]{20,})"
    r"|open\?id=(?P<open_id>[A-Za-z0-9_-]{20,}))",
    re.IGNORECASE,
)

_KIND_LABEL = {
    "file": "file",
    "document": "document",
    "spreadsheets": "spreadsheet",
    "presentation": "presentation",
}


def _extract_drive_links(payload: dict) -> list[dict]:
    """Walk text/plain and text/html parts for Drive file URLs.

    Emails often deliver "Drive attachments" as inline links rather than MIME
    parts with an attachmentId. Agents can chain drive_download_file on each
    file_id returned here.
    """
    seen: dict[str, str] = {}

    def visit(node: dict) -> None:
        mime = node.get("mimeType", "")
        if mime in ("text/plain", "text/html"):
            raw = _decode_part_data(node.get("body", {}).get("data", ""))
            for match in _DRIVE_URL_RE.finditer(raw):
                fid = match.group("id") or match.group("open_id")
                if not fid:
                    continue
                kind = _KIND_LABEL.get((match.group("kind") or "").lower(), "file")
                seen.setdefault(fid, kind)
        for child in node.get("parts", []) or []:
            visit(child)

    visit(payload)
    return [{"file_id": fid, "kind": kind} for fid, kind in seen.items()]


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
            {"id": lbl["id"], "name": lbl.get("name", ""), "type": lbl.get("type", "user")}
            for lbl in data.get("labels", [])
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
            "drive_links": _extract_drive_links(payload),
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
        "drive_links": _extract_drive_links(payload),
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


def _safe_filename(name: str) -> str:
    """Strip path separators and keep the basename only."""
    return os.path.basename(name).replace("/", "_").replace("\\", "_") or "file"


async def gmail_download_attachment(
    message_id: str,
    attachment_id: str,
    filename: str = "",
    mime: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Download a Gmail attachment to the local cache.

    Saves to ./tmp/gmail_attachments/{message_id}_{filename}. All tmp dirs are
    swept at the start of this call — files older than TMP_STORAGE_TTL_HOURS
    (default 24) are removed.

    For convertible MIME types (xlsx/csv/docx/pptx/rtf/txt/md/json/yaml), the
    extracted text is included as `extracted_text` so the agent can read the
    content directly. Non-convertible binaries (PDF/images/etc.) return only
    metadata; run `analyze_data` on `file_path` for deeper inspection.

    Args:
        message_id: Gmail message ID the attachment belongs to.
        attachment_id: Attachment ID (from gmail_get_message).
        filename: Optional original filename. Falls back to attachment_id if empty.
        mime: Attachment MIME type (from gmail_get_message). If omitted, guessed from filename.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Gmail not connected for {email}. Use google_connect first."}

    sweep_tmp()
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

        resolved_mime = mime or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        extracted_text = None
        if is_convertible(resolved_mime):
            extracted_text = to_text(content, resolved_mime, safe_name)

        artifact_name = None
        if tool_context and _is_artifact_inline(resolved_mime):
            part = types.Part.from_bytes(data=content, mime_type=resolved_mime)
            try:
                await tool_context.save_artifact(safe_name, part)
                artifact_name = safe_name
            except Exception as exc:
                logger.warning("save_artifact failed for %s: %s", safe_name, exc)

        return {
            "status": "success",
            "file_path": out_path,
            "filename": safe_name,
            "mime": resolved_mime,
            "size_bytes": len(content),
            "extracted_text": extracted_text,
            "artifact_name": artifact_name,
        }
    except Exception as e:
        return {"status": "error", "message": f"Gmail API error: {e}"}


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
            last_date = d
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
