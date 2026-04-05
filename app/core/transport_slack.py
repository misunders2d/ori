import logging
import os
import re
import httpx
from typing import Optional, Tuple
from urllib.parse import urlparse

from app.core.transport import TransportAdapter

logger = logging.getLogger(__name__)

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
                "channel_id": session_id.replace("sl_", ""),
            }
        return {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json; charset=utf-8"
        }

    async def send_message(self, target_id: str | int, text: str) -> None:
        url = SLACK_API_URL.format(method="chat.postMessage")

        # SECURITY: Scrub any leaked secrets before they reach the user
        text = _scrub_secrets(text)

        payload = {
            "channel": str(target_id),
            "text": text
        }
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

    async def delete_message(self, target_id: str | int, message_id: int | str) -> None:
        url = SLACK_API_URL.format(method="chat.delete")
        payload = {
            "channel": str(target_id),
            "ts": str(message_id)
        }
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
            data = resp.json()
            if not data.get("ok"):
                logger.warning("Slack deleteMessage failed: %s", data.get("error", data))
        except Exception:
            logger.exception("Failed to delete Slack message %s in %s", message_id, target_id)

    async def send_media(self, target_id: str | int, data: bytes, mime_type: str, caption: str = "") -> None:
        url = SLACK_API_URL.format(method="files.upload")
        headers = {"Authorization": f"Bearer {self._token}"}

        import mimetypes
        ext = mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"file{ext}"

        # Scrub caption for secrets
        if caption:
            caption = _scrub_secrets(caption)

        data_dict = {
            "channels": str(target_id),
            "initial_comment": caption
        }
        files = {
            "file": (filename, data, mime_type)
        }

        try:
            resp = await self._client.post(url, data=data_dict, files=files, headers=headers)
            json_data = resp.json()
            if not json_data.get("ok"):
                logger.error("Slack send_media failed: %s", json_data.get("error", json_data))
        except Exception:
            logger.exception("Failed to upload media to Slack channel %s", target_id)

    async def download_file(self, file_url: str) -> Optional[Tuple[bytes, str, str]]:
        # Validate URL is from Slack servers to prevent SSRF
        parsed = urlparse(file_url)
        if parsed.hostname and not parsed.hostname.endswith(".slack.com"):
            logger.error("Refusing to download non-Slack file URL: %s", file_url)
            return None

        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._client.get(file_url, headers=headers)
            if resp.status_code != 200:
                logger.error("Failed to download Slack file from %s, status: %s", file_url, resp.status_code)
                return None

            # Enforce file size limit
            if len(resp.content) > _MAX_DOWNLOAD_BYTES:
                logger.warning("Slack file too large (%d bytes), skipping: %s", len(resp.content), file_url)
                return None

            import mimetypes
            import os as _os
            filename = _os.path.basename(parsed.path)
            mime_type, _ = mimetypes.guess_type(filename)
            return resp.content, mime_type or "application/octet-stream", filename
        except Exception:
            logger.exception("Error downloading Slack file from %s", file_url)
        return None
