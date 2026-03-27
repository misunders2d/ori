import asyncio
import logging
import mimetypes
import os
from typing import Optional

import httpx
from google.genai import types

from app.core.agent_executor import (
    extract_agent_response,
    process_message_for_context,
    update_session_state,
)
from app.core.transport import TransportAdapter, register_adapter

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"


class TelegramAdapter(TransportAdapter):
    """Telegram implementation of the transport adapter."""

    def __init__(self, client: httpx.AsyncClient, token: str):
        self._client = client
        self._token = token

    @property
    def platform_name(self) -> str:
        return "telegram"

    def make_session_id(self, chat_id: str | int) -> str:
        return f"tg_chat_{chat_id}"

    def make_user_id(self, user_id: str | int) -> str:
        return f"tg_{user_id}"

    def parse_notify_info(self, session_id: str) -> dict:
        if session_id.startswith("tg_chat_"):
            try:
                return {"type": "telegram", "chat_id": int(session_id.replace("tg_chat_", ""))}
            except ValueError:
                pass
        return {}

    async def send_message(self, chat_id: str | int, text: str) -> None:
        url = TELEGRAM_API.format(token=self._token, method="sendMessage")
        
        # Safe chunking to handle the 4096 character limit
        limit = 4000
        chunks = []
        remaining = text
        while len(remaining) > limit:
            split_at = remaining.rfind('\n', 0, limit)
            if split_at == -1:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip('\n')
        if remaining:
            chunks.append(remaining)

        for chunk in chunks:
            if not chunk.strip(): continue
            try:
                resp = await self._client.post(
                    url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
                )
                if resp.status_code != 200:
                    # Markdown was rejected — retry without it
                    resp = await self._client.post(url, json={"chat_id": chat_id, "text": chunk})
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
            await self._client.post(url, json={"chat_id": chat_id, "message_id": message_id})
        except Exception:
            pass  # May fail if bot lacks permissions, non-critical

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



async def poll_telegram(get_runner_fn, process_init_fn):
    """Long-poll Telegram's getUpdates API and process messages."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("  Telegram: No bot token configured, poller disabled")
        return

    if os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
        print("  Telegram: Webhook mode (polling disabled)")
        return

    async with httpx.AsyncClient(timeout=10) as setup_client:
        url = TELEGRAM_API.format(token=token, method="deleteWebhook")
        try:
            resp = await setup_client.post(url)
            logger.info("Deleted existing Telegram webhook: %s", resp.json().get("ok"))
        except Exception:
            logger.warning("Could not delete Telegram webhook, polling may not work")

        # Fetch bot info to get username for mention detection
        bot_username = ""
        try:
            bot_info_resp = await setup_client.get(TELEGRAM_API.format(token=token, method="getMe"))
            bot_info = bot_info_resp.json().get("result", {})
            bot_username = bot_info.get("username", "")
            logger.info("Telegram Bot Username: %s", bot_username)
        except Exception:
            logger.warning("Could not fetch bot information for mention detection.")

    print("  Telegram: Polling mode active — listening for messages...")
    offset_file = os.path.join(os.path.abspath("./data"), ".tg_poll_offset")
    offset = 0
    try:
        with open(offset_file) as f:
            offset = int(f.read().strip())
            logger.info("Resumed Telegram poll offset: %d", offset)
    except (FileNotFoundError, ValueError):
        pass

    async with httpx.AsyncClient(timeout=60) as client:
        # Register the Telegram adapter so scheduled tasks and tools can route back
        adapter = TelegramAdapter(client, token)
        register_adapter(adapter)

        _active_tasks = {}
        _media_group_buffers = {}
        _media_group_timers = {}

        async def _process_and_send(_runner, _session_user_id, _session_id, _message_content, _user_id, _chat_id):
            async def keep_typing(__chat_id=_chat_id):
                while True:
                    await adapter.send_typing(__chat_id)
                    await asyncio.sleep(4)
            
            typing_task = asyncio.create_task(keep_typing())
            try:
                response = await extract_agent_response(
                    _runner, _session_user_id, _session_id, _message_content, _user_id
                )
                await adapter.send_message(_chat_id, response)
            except asyncio.CancelledError:
                pass
            finally:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
                if _session_id in _active_tasks and _active_tasks[_session_id] == asyncio.current_task():
                    del _active_tasks[_session_id]

        async def flush_media_group(mg_id, _runner, _session_user_id, _session_id, _user_id, _chat_id):
            await asyncio.sleep(1.5)
            if mg_id not in _media_group_buffers:
                return
            
            combined_parts = _media_group_buffers.pop(mg_id)
            _media_group_timers.pop(mg_id, None)
            
            combined_content = types.Content(role="user", parts=combined_parts)
            
            if _session_id in _active_tasks and not _active_tasks[_session_id].done():
                _active_tasks[_session_id].cancel()
                await adapter.send_message(_chat_id, "Aborting previous task to prioritize new grouped media...")
                
            task = asyncio.create_task(
                _process_and_send(_runner, _session_user_id, _session_id, combined_content, _user_id, _chat_id)
            )
            _active_tasks[_session_id] = task

        while True:
            try:
                url = TELEGRAM_API.format(token=token, method="getUpdates")
                resp = await client.get(url, params={"offset": offset, "timeout": 30})
                data = resp.json()

                if not data.get("ok"):
                    logger.warning("Telegram getUpdates returned error: %s", data)
                    await asyncio.sleep(5)
                    continue

                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    try:
                        with open(offset_file, "w") as f:
                            f.write(str(offset))
                    except OSError:
                        pass
                    msg = update.get("message")
                    if not msg:
                        continue

                    text = msg.get("text", msg.get("caption", ""))
                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    chat_type = chat.get("type", "private")
                    message_id = msg["message_id"]
                    from_user = msg.get("from", {})

                    display_name = from_user.get("first_name", "Unknown")
                    if from_user.get("last_name"):
                        display_name += f" {from_user['last_name']}"

                    user_id = adapter.make_user_id(from_user.get("id", "unknown"))
                    session_user_id = adapter.make_session_id(chat_id)
                    session_id = adapter.make_session_id(chat_id)

                    # File handling
                    file_id = None
                    file_info_text = ""
                    if "photo" in msg:
                        file_id = msg["photo"][-1]["file_id"]
                        file_info_text = "[Photo]"
                    elif "document" in msg:
                        file_id = msg["document"]["file_id"]
                        file_info_text = f"[Document: {msg['document'].get('file_name', 'unknown')}]"
                    elif "voice" in msg:
                        file_id = msg["voice"]["file_id"]
                        file_info_text = "[Voice Message]"
                    elif "audio" in msg:
                        file_id = msg["audio"]["file_id"]
                        file_info_text = f"[Audio: {msg['audio'].get('title', 'unknown')}]"
                    
                    # Prevent empty messages without files
                    if not text and not file_id:
                        continue

                    # Suppress the redundant "[Photo]" tag for every single photo in a media group to prevent pollution
                    mg_id = msg.get("media_group_id")
                    if mg_id and not text:
                        enriched_text = ""
                    else:
                        enriched_text = f"Message from {display_name} ({user_id}): {text} {file_info_text}".strip()

                    message_content = types.Content(role="user", parts=[])
                    if enriched_text:
                        message_content.parts.append(types.Part.from_text(text=enriched_text))

                    if file_id:
                        file_data = await adapter.download_file(file_id)
                        if file_data:
                            blob_bytes, mime_type, filename = file_data
                            message_content.parts.append(
                                types.Part(inline_data=types.Blob(data=blob_bytes, mime_type=mime_type))
                            )

                    # SECURE KEY CAPTURE: intercept before anything reaches the agent
                    from app.secure_config import capture_key, check_pending

                    if check_pending(session_id):
                        if text:
                            result = capture_key(session_id, text)
                            await adapter.delete_message(chat_id, message_id)
                            await adapter.send_message(chat_id, result["message"])
                        continue

                    # Handle /init command
                    if text.strip().startswith("/init"):
                        result = process_init_fn(text)
                        await adapter.send_message(chat_id, result)
                        continue

                    # Handle /start command
                    if text.strip() == "/start":
                        await adapter.send_message(
                            chat_id,
                            "Welcome to the Ori Daemon! Send me a message to get started.",
                        )
                        continue

                    runner = get_runner_fn()
                    if not runner:
                        await adapter.send_message(
                            chat_id,
                            "Bot is not fully configured yet. Please visit the /setup page.",
                        )
                        continue

                    # Upstream Access Control 
                    allowed_users_str = os.environ.get("ALLOWED_USER_IDS", "")
                    allowed_users = [u.strip() for u in allowed_users_str.split(",") if u.strip()]
                    
                    if allowed_users and user_id not in allowed_users:
                        logger.warning("Unauthorized access attempt by %s in chat %s", user_id, chat_id)
                        await adapter.send_message(chat_id, "⛔ You are not authorized to interact with this agent.")
                        continue


                    is_group = chat_type in ["group", "supergroup", "channel"]
                    is_mentioned = bot_username and (f"@{bot_username}" in text)
                    if is_group and not is_mentioned:
                        logger.info("Silently adding group message for context to session %s", session_id)
                        asyncio.create_task(process_message_for_context(runner, session_user_id, session_id, message_content))
                        continue
                        
                    if mg_id:
                        if mg_id not in _media_group_buffers:
                            _media_group_buffers[mg_id] = []
                        _media_group_buffers[mg_id].extend(message_content.parts)
                        
                        if mg_id in _media_group_timers:
                            _media_group_timers[mg_id].cancel()
                        _media_group_timers[mg_id] = asyncio.create_task(
                            flush_media_group(mg_id, runner, session_user_id, session_id, user_id, chat_id)
                        )
                        continue

                    # Mid-flight Cancellation Logic
                    if session_id in _active_tasks and not _active_tasks[session_id].done():
                        _active_tasks[session_id].cancel()
                        if text.strip().lower() in ["cancel", "stop", "abort", "nevermind"]:
                            await adapter.send_message(chat_id, "Aborted previous request seamlessly.")
                            continue
                        else:
                            await adapter.send_message(chat_id, "Aborting previous task to prioritize new input...")

                    # Launch the agent response dynamically in the background mapping to the session
                    task = asyncio.create_task(
                        _process_and_send(runner, session_user_id, session_id, message_content, user_id, chat_id)
                    )
                    _active_tasks[session_id] = task

            except httpx.ReadTimeout:
                continue
            except asyncio.CancelledError:
                logger.info("Telegram poller shutting down")
                return
            except Exception:
                logger.exception("Telegram poller error, retrying in 5s")
                await asyncio.sleep(5)
