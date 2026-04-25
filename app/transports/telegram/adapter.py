"""Telegram transport adapter — pure adapter, no message-loop logic.

Implements `TransportAdapter` for Telegram Bot API: send_message (with
chunking + Markdown fallback + secret scrubbing), send_typing, send_media
(routes to sendPhoto/Audio/Video/Document by mime), download_file, and the
session/user id canonicalization.

Helpers `_scrub_secrets` and `_update_heartbeat` live here because they
are transport-layer concerns, used by both the adapter (scrub on send) and
the poller (heartbeat on each long-poll cycle).
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
from datetime import datetime
from typing import Optional

import httpx

from app.runtime.health import HEARTBEAT_FILE
from app.runtime.transport import TransportAdapter

logger = logging.getLogger(__name__)


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"


def _update_heartbeat() -> None:
    """Touch the heartbeat file so the health plugin can detect liveness."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(datetime.now().isoformat())
    except Exception:
        pass


# Env keys whose VALUES are sensitive (not public identifiers like GITHUB_REPO).
_SECRET_ENV_KEYS = (
    "GOOGLE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
)

# Common token formats — fallback for secrets the env doesn't know about yet.
_TOKEN_PATTERNS = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,})"
    r"|(?:ghp_[A-Za-z0-9]{36,})"
    r"|(?:gho_[A-Za-z0-9]{36,})"
    r"|(?:ghu_[A-Za-z0-9]{36,})"
    r"|(?:ghs_[A-Za-z0-9]{36,})"
    r"|(?:sk-[A-Za-z0-9]{20,})"
    r"|(?:AIzaSy[A-Za-z0-9_-]{33})"
)


def _scrub_secrets(text: str) -> str:
    """Redact known secret values and common token formats from outgoing text."""
    for key in _SECRET_ENV_KEYS:
        val = os.environ.get(key, "")
        if val and len(val) > 8 and val in text:
            text = text.replace(val, "[REDACTED]")
    return _TOKEN_PATTERNS.sub("[REDACTED]", text)


class TelegramAdapter(TransportAdapter):
    """TransportAdapter implementation for Telegram Bot API."""

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    @property
    def platform_name(self) -> str:
        return "telegram"

    def make_session_id(self, chat_id: str | int) -> str:
        return f"tg_{chat_id}"

    def make_user_id(self, user_id: str | int) -> str:
        return f"tg_{user_id}"

    def parse_notify_info(self, session_id: str) -> dict:
        if session_id.startswith("tg_"):
            try:
                return {"type": "telegram", "chat_id": int(session_id[3:])}
            except ValueError:
                pass
        return {}

    async def send_message(self, chat_id: str | int, text: str) -> None:
        url = TELEGRAM_API.format(token=self._token, method="sendMessage")
        text = _scrub_secrets(text)
        # Telegram caps text at 4096; chunk safely on newline boundaries.
        limit = 4000
        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            split_at = remaining.rfind("\n", 0, limit)
            if split_at == -1:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip("\n")
        if remaining:
            chunks.append(remaining)

        for chunk in chunks:
            if not chunk.strip():
                continue
            try:
                resp = await self._client.post(
                    url,
                    json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                )
                if resp.status_code != 200:
                    # Markdown rejected — retry plain text.
                    resp = await self._client.post(
                        url, json={"chat_id": chat_id, "text": chunk}
                    )
                    if resp.status_code != 200:
                        logger.error("Telegram sendMessage failed: %s", resp.text)
            except Exception:
                logger.exception("Failed to send Telegram message to chat %s", chat_id)

    async def send_typing(self, chat_id: str | int) -> None:
        url = TELEGRAM_API.format(token=self._token, method="sendChatAction")
        try:
            await self._client.post(url, json={"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    async def delete_message(self, chat_id: str | int, message_id: int) -> None:
        url = TELEGRAM_API.format(token=self._token, method="deleteMessage")
        try:
            await self._client.post(
                url, json={"chat_id": chat_id, "message_id": message_id}
            )
        except Exception:
            pass  # Bot may lack permissions; non-critical.

    async def send_media(
        self,
        chat_id: str | int,
        data: bytes,
        mime_type: str,
        caption: str = "",
    ) -> None:
        prefix = (mime_type or "").split("/")[0]
        if prefix == "image":
            method, field = "sendPhoto", "photo"
        elif prefix == "audio":
            method, field = "sendAudio", "audio"
        elif prefix == "video":
            method, field = "sendVideo", "video"
        else:
            method, field = "sendDocument", "document"

        ext = mimetypes.guess_extension(mime_type) or ""
        filename = f"file{ext}"

        url = TELEGRAM_API.format(token=self._token, method=method)
        form_data: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            form_data["caption"] = caption
        try:
            files = {field: (filename, data, mime_type)}
            resp = await self._client.post(url, data=form_data, files=files)
            if resp.status_code != 200:
                logger.error("Telegram %s failed: %s", method, resp.text)
        except Exception:
            logger.exception("Failed to send media to chat %s via %s", chat_id, method)

    async def download_file(self, file_id: str) -> Optional[tuple[bytes, str, str]]:
        try:
            url = TELEGRAM_API.format(token=self._token, method="getFile")
            resp = await self._client.get(url, params={"file_id": file_id})
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram getFile failed: %s", data)
                return None
            file_path = data["result"].get("file_path")
            if not file_path:
                return None
            download_url = TELEGRAM_FILE_API.format(token=self._token, path=file_path)
            file_resp = await self._client.get(download_url)
            if file_resp.status_code != 200:
                logger.error("Telegram file download failed: %d", file_resp.status_code)
                return None
            filename = os.path.basename(file_path)
            mime_type, _ = mimetypes.guess_type(filename)
            if not mime_type:
                mime_type = "application/octet-stream"
            return file_resp.content, mime_type, filename
        except Exception:
            logger.exception("Error downloading Telegram file %s", file_id)
            return None
