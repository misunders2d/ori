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
