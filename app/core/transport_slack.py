import logging
import httpx
from typing import Optional, Tuple

from app.core.transport import TransportAdapter

logger = logging.getLogger(__name__)

SLACK_API_URL = "https://slack.com/api/{method}"

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
        payload = {
            "channel": str(target_id),
            "text": text
        }
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
            data = resp.json()
            if not data.get("ok"):
                logger.error("Slack sendMessage failed: %s", data)
        except Exception:
            logger.exception("Failed to send Slack message to %s", target_id)

    async def send_typing(self, target_id: str | int) -> None:
        # Slack Web API does not support a REST-based typing indicator for bots.
        # Typing indicators in Slack generally require the RTM (Real Time Messaging) API.
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
                logger.warning("Slack deleteMessage failed: %s", data)
        except Exception:
            logger.exception("Failed to delete Slack message %s in %s", message_id, target_id)

    async def send_media(self, target_id: str | int, data: bytes, mime_type: str, caption: str = "") -> None:
        url = SLACK_API_URL.format(method="files.upload")
        # files.upload uses multipart/form-data, so headers shouldn't enforce application/json
        headers = {"Authorization": f"Bearer {self._token}"}
        
        import mimetypes
        ext = mimetypes.guess_extension(mime_type) or ".bin"
        filename = f"file{ext}"
        
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
                logger.error("Slack send_media failed: %s", json_data)
        except Exception:
            logger.exception("Failed to upload media to Slack channel %s", target_id)

    async def download_file(self, file_url: str) -> Optional[Tuple[bytes, str, str]]:
        # For Slack, the 'file_id' passed here will actually be the private URL_PRIVATE from the event
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = await self._client.get(file_url, headers=headers)
            if resp.status_code == 200:
                import mimetypes
                import os
                filename = os.path.basename(file_url.split("?")[0])
                mime_type, _ = mimetypes.guess_type(filename)
                return resp.content, mime_type or "application/octet-stream", filename
            else:
                logger.error("Failed to download Slack file from %s, status: %s", file_url, resp.status_code)
        except Exception:
            logger.exception("Error downloading Slack file from %s", file_url)
        return None
