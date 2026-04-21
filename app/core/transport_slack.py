import logging
import os
import random
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.core.transport import TransportAdapter

logger = logging.getLogger(__name__)

# Thinking/working indicator GIFs — shown while the agent processes a request
_THINKING_GIFS = [
    "https://media.giphy.com/media/l0HlBO7eyXzSZkJri/giphy.gif",
    "https://media.giphy.com/media/WoWm8YzFQJg5i/giphy.gif",
    "https://media.giphy.com/media/JIX9t2j0ZTN9S/giphy.gif",
    "https://media.giphy.com/media/l3nWhI38IWDofyDrW/giphy.gif",
    "https://media.giphy.com/media/tXL4FHPSnVJ0A/giphy.gif",
    "https://media.giphy.com/media/Yl5nlnrtpQIrI1AfhD/giphy.gif",
    "https://media.giphy.com/media/S675CRFMUX7el7gyu7/giphy.gif",
    "https://media.giphy.com/media/eGNRUOkpiBoJi8VQMN/giphy.gif",
    "https://media.giphy.com/media/L3X9GvVhP1nY23Ah6u/giphy.gif",
    "https://media.giphy.com/media/1FMaabePDEfgk/giphy.gif",
    "https://media.giphy.com/media/H6cmWzp6LGFvqjidB7/giphy.gif",
    "https://media.giphy.com/media/z4lwT4QTkK3sYITR7Z/giphy.gif",
    "https://media.giphy.com/media/L17xM7PvLcqJggsCYa/giphy.gif",
    "https://media.giphy.com/media/VFYJXIuuFl6pO/giphy.gif",
    "https://media.giphy.com/media/WRQBXSCnEFJIuxktnw/giphy.gif",
    "https://media.giphy.com/media/Qs1uMrvmHAKIUXxO2g/giphy.gif",
    "https://media.giphy.com/media/ZBK7b4vHYyb0n70zJq/giphy.gif",
    "https://media.giphy.com/media/APqEbxBsVlkWSuFpth/giphy.gif",
    "https://media.giphy.com/media/kDf0eEXhOhlZgdp2dy/giphy.gif",
    "https://media.giphy.com/media/Zxzr2pp6qU64g/giphy.gif",
    "https://media.giphy.com/media/xCwYFe19SldXLrJlwm/giphy.gif",
    "https://media.giphy.com/media/Nde7zlveRCWPu/giphy.gif",
]

SLACK_API_URL = "https://slack.com/api/{method}"

# Max file download size: 20 MB
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Secret scrubbing (mirrors telegram_poller pattern)
_SECRET_ENV_KEYS = {
    "GOOGLE_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
}

_TOKEN_PATTERNS = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,})"
    r"|(?:ghp_[A-Za-z0-9]{36,})"
    r"|(?:gho_[A-Za-z0-9]{36,})"
    r"|(?:ghu_[A-Za-z0-9]{36,})"
    r"|(?:ghs_[A-Za-z0-9]{36,})"
    r"|(?:sk-[A-Za-z0-9]{20,})"
    r"|(?:AIzaSy[A-Za-z0-9_-]{33})"
    r"|(?:xoxb-[A-Za-z0-9-]+)"
    r"|(?:xapp-[A-Za-z0-9-]+)"
)


def _scrub_secrets(text: str) -> str:
    """Redact known secret values and common token patterns from outgoing text."""
    for key in _SECRET_ENV_KEYS:
        val = os.environ.get(key, "")
        if val and len(val) > 8 and val in text:
            text = text.replace(val, "[REDACTED]")
    text = _TOKEN_PATTERNS.sub("[REDACTED]", text)
    return text


class SlackAdapter(TransportAdapter):
    """Slack implementation of the transport adapter (Web API)."""

    def __init__(self, client: httpx.AsyncClient, bot_token: str):
        self._client = client
        self._token = bot_token
        # Lazy-initialized Slack SDK client for file uploads
        self._sdk_client = None

    @property
    def platform_name(self) -> str:
        return "slack"

    def make_session_id(self, channel_id: str | int) -> str:
        return f"sl_{channel_id}"

    def make_user_id(self, user_id: str | int) -> str:
        return f"sl_{user_id}"

    def parse_notify_info(self, session_id: str) -> dict:
        if session_id.startswith("sl_"):
            return {
                "type": "slack",
                "chat_id": session_id.replace("sl_", ""),
            }
        return {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def send_message(
        self, target_id: str | int, text: str, *, thread_ts: str = ""
    ) -> None:
        url = SLACK_API_URL.format(method="chat.postMessage")

        # SECURITY: Scrub any leaked secrets before they reach the user
        text = _scrub_secrets(text)

        payload = {"channel": str(target_id), "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
            data = resp.json()
            if not data.get("ok"):
                logger.error("Slack sendMessage failed: %s", data.get("error", data))
        except Exception:
            logger.exception("Failed to send Slack message to %s", target_id)

    async def send_typing(self, target_id: str | int) -> None:
        # Slack Web API does not support a REST-based typing indicator for bots.
        pass

    async def send_thinking_indicator(
        self, target_id: str | int, *, thread_ts: str = ""
    ) -> str | None:
        """Post a random 'thinking' GIF and return its message ts for later deletion."""
        gif_url = random.choice(_THINKING_GIFS)
        url = SLACK_API_URL.format(method="chat.postMessage")
        payload = {
            "channel": str(target_id),
            "text": gif_url,
            "unfurl_media": True,
            "unfurl_links": True,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
            data = resp.json()
            if data.get("ok"):
                return data.get("ts")
            logger.error("Slack thinking indicator failed: %s", data.get("error", data))
        except Exception:
            logger.exception("Failed to send thinking indicator to %s", target_id)
        return None

    async def delete_message(self, target_id: str | int, message_id: int | str) -> None:
        url = SLACK_API_URL.format(method="chat.delete")
        payload = {"channel": str(target_id), "ts": str(message_id)}
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
            data = resp.json()
            if not data.get("ok"):
                logger.warning(
                    "Slack deleteMessage failed: %s", data.get("error", data)
                )
        except Exception:
            logger.exception(
                "Failed to delete Slack message %s in %s", message_id, target_id
            )

    async def send_media(
        self,
        target_id: str | int,
        data: bytes,
        mime_type: str,
        caption: str = "",
        *,
        thread_ts: str = "",
    ) -> None:
        """Upload a file using the Slack SDK's files_upload_v2 (handles the full 3-step flow)."""
        import mimetypes as _mt
        from datetime import datetime as _dt

        ext = _mt.guess_extension(mime_type) or ".bin"
        filename = f"attachment_{_dt.now().strftime('%Y%m%d_%H%M%S')}{ext}"

        if caption:
            caption = _scrub_secrets(caption)

        try:
            if self._sdk_client is None:
                from slack_sdk.web.async_client import AsyncWebClient

                self._sdk_client = AsyncWebClient(token=self._token)

            kwargs = {
                "content": data,
                "filename": filename,
                "title": filename,
                "channel": str(target_id),
            }
            if caption:
                kwargs["initial_comment"] = caption
            if thread_ts:
                kwargs["thread_ts"] = thread_ts

            resp = await self._sdk_client.files_upload_v2(**kwargs)
            if not resp.get("ok"):
                logger.error(
                    "Slack files_upload_v2 failed: %s", resp.get("error", resp)
                )
        except Exception:
            logger.exception("Failed to upload media to Slack channel %s", target_id)

    async def download_file(
        self,
        file_url: str,
        mime_hint: str = "",
        filename_hint: str = "",
    ) -> Optional[Tuple[bytes, str, str]]:
        """Download a Slack-hosted file with auth.

        Slack event payloads carry authoritative `mimetype` and `name` on each
        file object, so the poller should pass them in as hints — Slack's
        ``url_private`` is not guaranteed to contain the original filename or
        extension (some files surface as ID-only paths), which would make
        ``mimetypes.guess_type`` fall back to ``application/octet-stream`` and
        block downstream models (Gemini) from parsing PDFs, DOCX, etc.
        """
        # Validate URL is from Slack servers to prevent SSRF
        parsed = urlparse(file_url)
        if parsed.hostname and not parsed.hostname.endswith(".slack.com"):
            logger.error("Refusing to download non-Slack file URL: %s", file_url)
            return None

        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            # Slack file URLs redirect to their CDN (S3 / edge-hosted storage).
            # Default httpx behavior is to NOT follow redirects, which would
            # leave us with a 302 carrying no body. Explicit opt-in is needed.
            resp = await self._client.get(file_url, headers=headers, follow_redirects=True)
            if resp.status_code != 200:
                # Log response headers (redacted) on failure to distinguish
                # 403 scope issues from other errors; body may contain a
                # Slack error code like "missing_scope" or "not_authed".
                body_snippet = resp.text[:200] if resp.text else ""
                logger.error(
                    "Failed to download Slack file from %s, status: %s, body: %r",
                    file_url,
                    resp.status_code,
                    body_snippet,
                )
                return None

            # Enforce file size limit
            if len(resp.content) > _MAX_DOWNLOAD_BYTES:
                logger.warning(
                    "Slack file too large (%d bytes), skipping: %s",
                    len(resp.content),
                    file_url,
                )
                return None

            # Prefer hints from the Slack event (authoritative) over URL guesses.
            filename = filename_hint or ""
            mime_type = mime_hint or ""
            if not filename or not mime_type:
                import mimetypes
                import os as _os
                if not filename:
                    filename = _os.path.basename(parsed.path) or "attachment"
                if not mime_type:
                    guessed, _ = mimetypes.guess_type(filename)
                    mime_type = guessed or ""
            return resp.content, mime_type or "application/octet-stream", filename
        except Exception:
            logger.exception("Error downloading Slack file from %s", file_url)
        return None
