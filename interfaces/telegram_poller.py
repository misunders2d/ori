import asyncio
import logging
import mimetypes
import os
import re
import weakref
from datetime import datetime
from typing import Optional

import httpx
from google.genai import types

from app.core.agent_executor import (
    _inject_metadata_header,
    extract_agent_response,
    process_message_for_context,
)
from app.core.transport import TransportAdapter, register_adapter

# New imports for whitelist and logging
from app.core.whitelist import is_allowed, is_blacklisted, should_notify_admin, whitelist_chat, reload as reload_whitelist
from app.core.channel_logger import log_message
from app.core.roster import record_user

logger = logging.getLogger(__name__)

# Heartbeat file for self-diagnostics
HEARTBEAT_FILE = os.path.abspath("./data/.tg_heartbeat")


def _update_heartbeat():
    """Updates the heartbeat file with current timestamp."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(datetime.now().isoformat())
    except Exception:
        pass


# Keys whose env values are sensitive secrets (not public identifiers like GITHUB_REPO)
_SECRET_ENV_KEYS = {
    "GOOGLE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
}

# Common token patterns as a fallback (catches secrets the env doesn't know about yet)
_TOKEN_PATTERNS = re.compile(
    r"(?:github_pat_[A-Za-z0-9_]{20,})"
    r"|(?:ghp_[A-Za-z0-9]{36,})"
    r"|(?:gho_[A-Za-z0-9]{36,})"
    r"|(?:ghu_[A-Za-z0-9]{36,})"
    r"|(?:ghs_[A-Za-z0-9]{36,})"
    r"|(?:sk-[A-Za-z0-9]{20,})"
    r"|(?:AIzaSy[A-Za-z0-9_-]{33})"
)


# Matches `http(s)://...` until whitespace, brackets, parens, backticks,
# or angle brackets (which mark markdown boundaries). Trailing punctuation
# like `.,;:!?` is handled in the replacement function below.
_URL_RE = re.compile(r"https?://[^\s<>\[\]\(\)`]+")
# Trailing punctuation that's almost never part of a real URL.
_URL_TAIL_PUNCT = ".,;:!?"


def _escape_urls_for_telegram_md(text: str) -> str:
    """Escape underscores/asterisks/backticks INSIDE detected URLs so legacy
    Telegram Markdown doesn't mangle URL params as italic/bold/code.

    Production failure 2026-05-12: Google OAuth URL came through with
    `client_id=...&redirect_uri=...&code_challenge=...`; Telegram's
    legacy Markdown parser saw `_text_` between underscores and emitted
    italic, stripping the underscores from the rendered link. Result:
    Google returned `Error 400: Required parameter is missing: response_type`.

    Only mutates substrings the URL regex matches. Other markdown
    formatting in the message body stays untouched. `\\_` etc render as
    a literal underscore in Telegram legacy Markdown, so URLs stay
    auto-clickable AND correct.
    """
    def _replace(m: re.Match) -> str:
        url = m.group(0)
        trail = ""
        while url and url[-1] in _URL_TAIL_PUNCT:
            trail = url[-1] + trail
            url = url[:-1]
        # Escape only the legacy-Markdown markers that can appear in URLs
        # (Telegram URL chars don't include `*` / `` ` `` legitimately,
        # but cover them defensively).
        safe = url.replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        return safe + trail

    return _URL_RE.sub(_replace, text)


def _scrub_secrets(text: str) -> str:
    """Redact known secret values and common token patterns from outgoing text."""
    # Layer 1: Redact actual configured secret values from env
    for key in _SECRET_ENV_KEYS:
        val = os.environ.get(key, "")
        if val and len(val) > 8 and val in text:
            text = text.replace(val, f"[REDACTED]")

    # Layer 2: Regex fallback for common token formats
    text = _TOKEN_PATTERNS.sub("[REDACTED]", text)

    return text


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
        return f"tg_{chat_id}"

    def make_user_id(self, user_id: str | int) -> str:
        return f"tg_{user_id}"

    def parse_notify_info(self, session_id: str) -> dict:
        if session_id.startswith("tg_"):
            try:
                return {
                    "type": "telegram",
                    "chat_id": int(session_id.replace("tg_", "")),
                }
            except ValueError:
                pass
        return {}

    async def send_message(self, chat_id: str | int, text: str) -> None:
        url = TELEGRAM_API.format(token=self._token, method="sendMessage")

        # SECURITY: Scrub any leaked secrets before they reach the user
        text = _scrub_secrets(text)

        # Markdown-safe URLs: legacy Telegram Markdown treats `_text_` as
        # italic and strips underscores from URL params (production proof
        # 2026-05-12: Google OAuth URL rendered with `clientid` / `responsetype`
        # instead of `client_id` / `response_type`). Escape `_`/`*`/`` ` ``
        # inside URL substrings only — leaves the rest of the markdown
        # formatting intact.
        text = _escape_urls_for_telegram_md(text)

        # Safe chunking to handle the 4096 character limit
        limit = 4000
        chunks = []
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
                    # Markdown was rejected — retry without it
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
            pass  # May fail if bot lacks permissions, non-critical

    async def send_media(
        self, chat_id: str | int, data: bytes, mime_type: str, caption: str = ""
    ) -> None:
        # Map MIME type to the appropriate Telegram method and form field
        mime_prefix = mime_type.split("/")[0] if mime_type else ""
        if mime_prefix == "image":
            method, field = "sendPhoto", "photo"
        elif mime_prefix == "audio":
            method, field = "sendAudio", "audio"
        elif mime_prefix == "video":
            method, field = "sendVideo", "video"
        else:
            method, field = "sendDocument", "document"

        # Derive a sensible filename from the MIME type
        from datetime import datetime as _dt
        ext = mimetypes.guess_extension(mime_type) or ""
        filename = f"attachment_{_dt.now().strftime('%Y%m%d_%H%M%S')}{ext}"

        url = TELEGRAM_API.format(token=self._token, method=method)
        form_data = {"chat_id": str(chat_id)}
        if caption:
            form_data["caption"] = caption

        try:
            files = {field: (filename, data, mime_type)}
            resp = await self._client.post(url, data=form_data, files=files)
            if resp.status_code != 200:
                logger.error("Telegram %s failed: %s", method, resp.text)
        except Exception:
            logger.exception("Failed to send media to chat %s via %s", chat_id, method)

    # ----------------------------------------------------------------------
    # Strict variants — slice 3 stubs (raise so the ABC does not block
    # instantiation). Slice 4 replaces these with real implementations
    # backed by ``_post`` and the outbound-files cache.
    # ----------------------------------------------------------------------

    async def send_text_strict(self, target_id: str | int, text: str) -> dict:
        raise NotImplementedError(
            "TelegramAdapter.send_text_strict not yet implemented (slice 4)"
        )

    async def send_media_strict(
        self,
        target_id: str | int,
        data: bytes | None,
        mime_type: str,
        caption: str = "",
        *,
        file_id: str | None = None,
        file_type: str | None = None,
        file_ref: str | None = None,
        owner_user_id: str | None = None,
        file_path: str | None = None,
    ) -> dict:
        raise NotImplementedError(
            "TelegramAdapter.send_media_strict not yet implemented (slice 4)"
        )

    async def copy_message_strict(
        self,
        target_id: str | int,
        from_chat_id: int,
        message_id: int,
    ) -> dict:
        raise NotImplementedError(
            "TelegramAdapter.copy_message_strict not yet implemented (slice 4)"
        )

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
            bot_info_resp = await setup_client.get(
                TELEGRAM_API.format(token=token, method="getMe")
            )
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

        _active_tasks = {}  # session_id -> (asyncio.Task, message_content)
        _media_group_buffers = {}
        _media_group_timers = {}
        _session_locks = weakref.WeakValueDictionary()  # session_id -> asyncio.Lock

        async def _get_lock(sid):
            lock = _session_locks.get(sid)
            if lock is None:
                lock = asyncio.Lock()
                _session_locks[sid] = lock
            return lock

        async def _process_and_send(
            _runner, _session_user_id, _session_id, _message_content, _user_id, _chat_id
        ):
            async with await _get_lock(_session_id):

                async def keep_typing(__chat_id=_chat_id):
                    while True:
                        await adapter.send_typing(__chat_id)
                        await asyncio.sleep(4)

                typing_task = asyncio.create_task(keep_typing())
                try:
                    response = await extract_agent_response(
                        _runner,
                        _session_user_id,
                        _session_id,
                        _message_content,
                        _user_id,
                    )
                    # Send text response (strip metadata prefix if the model echoed it)
                    if response.text:
                        import re
                        clean_text = re.sub(r"^Metadata:.*?\n", "", response.text).lstrip()
                        await adapter.send_message(_chat_id, clean_text or response.text)
                    # Send any media attachments the agent produced
                    for media_item in response.media_items:
                        await adapter.send_media(
                            _chat_id,
                            media_item["data"],
                            media_item["mime_type"],
                        )
                except asyncio.CancelledError:
                    pass
                finally:
                    typing_task.cancel()
                    try:
                        await typing_task
                    except asyncio.CancelledError:
                        pass
                    if (
                        _session_id in _active_tasks
                        and _active_tasks[_session_id][0] == asyncio.current_task()
                    ):
                        del _active_tasks[_session_id]

        async def _save_context(
            _runner, _session_user_id, _session_id, _message_content
        ):
            """Helper to save a message to history sequentially."""
            async with await _get_lock(_session_id):
                try:
                    await process_message_for_context(
                        _runner, _session_user_id, _session_id, _message_content
                    )
                except Exception:
                    logger.exception(
                        "Failed to save message to context for session %s", _session_id
                    )

        async def flush_media_group(
            mg_id, _runner, _session_user_id, _session_id, _user_id, _chat_id
        ):
            await asyncio.sleep(1.5)
            if mg_id not in _media_group_buffers:
                return

            combined_parts = _media_group_buffers.pop(mg_id)
            _media_group_timers.pop(mg_id, None)

            combined_content = types.Content(role="user", parts=combined_parts)

            if (
                _session_id in _active_tasks
                and not _active_tasks[_session_id][0].done()
            ):
                prev_task, prev_msg = _active_tasks[_session_id]
                prev_task.cancel()
                asyncio.create_task(
                    _save_context(_runner, _session_user_id, _session_id, prev_msg)
                )
                await adapter.send_message(
                    _chat_id,
                    "Aborting previous task to prioritize new grouped media...",
                )

            task = asyncio.create_task(
                _process_and_send(
                    _runner,
                    _session_user_id,
                    _session_id,
                    combined_content,
                    _user_id,
                    _chat_id,
                )
            )
            _active_tasks[_session_id] = (task, combined_content)

        while True:
            _update_heartbeat()
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
                    msg = update.get("message") or update.get("channel_post")
                    if not msg:
                        continue

                    text = msg.get("text", msg.get("caption", ""))
                    chat = msg.get("chat", {})
                    chat_id = chat.get("id")
                    chat_type = chat.get("type", "private")
                    is_group = chat_type in ["group", "supergroup"]
                    message_id = msg["message_id"]
                    from_user = msg.get("from", {})
                    
                    # --- UME: Extract platform-native UTC timestamp ---
                    unix_ts = msg.get("date", int(datetime.now().timestamp()))
                    msg_timestamp = datetime.utcfromtimestamp(unix_ts)

                    display_name = from_user.get("first_name", "Unknown")
                    if from_user.get("last_name"):
                        display_name += f" {from_user['last_name']}"

                    user_id = adapter.make_user_id(from_user.get("id", "unknown"))
                    session_id = adapter.make_session_id(chat_id)
                    session_user_id = session_id  # Use chat_id for session context isolation

                    # ── CHANNEL LOGGING (Moved before gate) ──────────────────
                    if chat_type == "channel":
                        if is_allowed(session_id):
                            log_message(session_id, user_id, display_name, text)
                        continue

                    # ── ACCESS CONTROL GATE ──────────────────────────────────
                    user_authorized = is_allowed(user_id)
                    group_authorized = is_group and is_allowed(session_id)
                    
                    if not (user_authorized or group_authorized):
                        if is_blacklisted(user_id) or is_blacklisted(session_id):
                            continue
                        
                        if text.strip() == "/start":
                            await adapter.send_message(
                                chat_id,
                                f"⛔ You are not authorized.\n\n"
                                f"Your User ID: `{user_id}`\n"
                                f"This Chat ID: `{session_id}`\n\n"
                                f"Please provide these IDs to the bot owner for access."
                            )
                            continue

                        if should_notify_admin(user_id):
                            admin_ids_str = os.environ.get("ADMIN_USER_IDS", "") or os.environ.get("ALLOWED_USER_IDS", "")
                            admin_ids = [u.strip() for u in admin_ids_str.split(",") if u.strip()]
                            for admin_id in admin_ids:
                                if admin_id.startswith("tg_"):
                                    admin_chat_id = admin_id.replace("tg_", "")
                                    await adapter.send_message(
                                        admin_chat_id,
                                        f"📢 **Unauthorized Access Attempt**\n\n"
                                        f"**User:** {display_name} ({user_id})\n"
                                        f"**Chat:** {chat_type} ({session_id})\n"
                                        f"**Message:** {text}\n\n"
                                        f"To allow, reply with: `Whitelist {user_id}`\n"
                                        f"To block, reply with: `Blacklist {user_id}`"
                                    )
                        continue

                    # Roster: capture every authorized inbound sender so the agent
                    # can later DM them by name (Telegram bots cannot initiate DMs;
                    # the user must have messaged the bot at least once first).
                    if not is_group:
                        record_user(
                            user_id=user_id,
                            platform="telegram",
                            chat_id=chat_id,
                            first_name=from_user.get("first_name", "") or "",
                            last_name=from_user.get("last_name", "") or "",
                            username=from_user.get("username", "") or "",
                        )

                    # File handling
                    file_id = None
                    file_info_text = ""
                    if "photo" in msg:
                        file_id = msg["photo"][-1]["file_id"]
                        file_info_text = "[Photo]"
                    elif "document" in msg:
                        file_id = msg["document"]["file_id"]
                        file_info_text = (
                            f"[Document: {msg['document'].get('file_name', 'unknown')}]"
                        )
                    elif "voice" in msg:
                        file_id = msg["voice"]["file_id"]
                        file_info_text = "[Voice Message]"
                    elif "audio" in msg:
                        file_id = msg["audio"]["file_id"]
                        file_info_text = (
                            f"[Audio: {msg['audio'].get('title', 'unknown')}]"
                        )
                    elif "video" in msg:
                        file_id = msg["video"]["file_id"]
                        file_info_text = (
                            f"[Video: {msg['video'].get('file_name', 'unknown')}]"
                        )
                    elif "video_note" in msg:
                        file_id = msg["video_note"]["file_id"]
                        file_info_text = "[Video Message]"
                    elif "contact" in msg:
                        c = msg["contact"]
                        cname = (f"{c.get('first_name', '')} {c.get('last_name', '')}").strip()
                        bits = [b for b in (cname, c.get("phone_number"), f"tg_user_id={c.get('user_id')}" if c.get("user_id") else None) if b]
                        file_info_text = f"[Contact: {', '.join(bits)}]"
                        if c.get("vcard"):
                            file_info_text += f"\nvCard:\n{c['vcard']}"
                    elif "location" in msg:
                        loc = msg["location"]
                        file_info_text = f"[Location: lat={loc.get('latitude')}, lon={loc.get('longitude')}]"
                    elif "venue" in msg:
                        v = msg["venue"]
                        file_info_text = f"[Venue: {v.get('title', '')} — {v.get('address', '')}]"

                    # Prevent empty messages without files
                    if not text and not file_id and not file_info_text:
                        continue

                    # --- DETERMINISTIC APPROVAL INTERCEPT ---
                    # Pre-LLM short-circuit: ``Approve ACT-XXXXXX
                    # [123456]`` from the user executes the staged
                    # action directly. Bypasses the agent so the LLM
                    # can't re-loop the approval gate by re-invoking
                    # the original gated tool. See
                    # ``app/core/approval_intercept.py``.
                    from app.core.approval_intercept import (
                        handle_approval,
                        parse_approval_text,
                    )

                    parsed_approval = parse_approval_text(text)
                    if parsed_approval:
                        approval_token, approval_totp = parsed_approval
                        try:
                            reply_text = await handle_approval(
                                token=approval_token,
                                totp_code=approval_totp,
                                user_id=user_id,
                                session_id=session_id,
                            )
                        except Exception as e:
                            logger.exception("approval intercept crashed")
                            reply_text = (
                                f"Approval `{approval_token}` could not "
                                f"be processed: {e}"
                            )
                        try:
                            await adapter.send_message(chat_id, reply_text)
                        except Exception:
                            logger.exception(
                                "failed to deliver approval result to chat %s",
                                chat_id,
                            )
                        continue  # skip agent for this update

                    # Construct normalized text with metadata header
                    mg_id = msg.get("media_group_id")
                    if mg_id and not text:
                        # Redundant tag suppression for media groups
                        enriched_text = ""
                    else:
                        raw_text = f"Message from {display_name} ({user_id}): {text} {file_info_text}".strip()
                        enriched_text = _inject_metadata_header(raw_text, msg_timestamp, "telegram")

                    message_content = types.Content(role="user", parts=[])
                    if enriched_text:
                        message_content.parts.append(
                            types.Part.from_text(text=enriched_text)
                        )

                    if file_id:
                        file_data = await adapter.download_file(file_id)
                        if file_data:
                            blob_bytes, mime_type, filename = file_data
                            from app.app_utils.file_convert import prepare_for_llm, save_upload
                            saved_path = save_upload(blob_bytes, filename)
                            prepared = prepare_for_llm(blob_bytes, mime_type, filename, saved_path)
                            message_content.parts.append(types.Part.from_text(text=prepared.text))
                            if prepared.inline_blob:
                                _blob_bytes, _blob_mime = prepared.inline_blob
                                message_content.parts.append(
                                    types.Part(
                                        inline_data=types.Blob(
                                            data=_blob_bytes, mime_type=_blob_mime
                                        )
                                    )
                                )

                    # Handle whitelist/blacklist shortcuts from authorized users
                    if is_allowed(user_id):
                        if text.lower().startswith("whitelist "):
                            target_id = text.split(" ")[1].strip()
                            whitelist_chat(target_id)
                            await adapter.send_message(chat_id, f"✅ Added `{target_id}` to whitelist.")
                            continue
                        elif text.lower().startswith("blacklist "):
                            target_id = text.split(" ")[1].strip()
                            from app.core.whitelist import blacklist_chat as _bl
                            _bl(target_id)
                            await adapter.send_message(chat_id, f"🌑 Added `{target_id}` to blacklist.")
                            continue

                    # Handle /start command — welcome message
                    bot_name = os.environ.get("BOT_NAME", "Ori")

                    # /chatid — print this chat's session_id (useful in groups/channels
                    # where the chat_id is otherwise opaque). Cheap, no LLM round-trip.
                    if text.strip() == "/chatid":
                        await adapter.send_message(
                            chat_id,
                            f"Chat ID: `{session_id}`\n"
                            f"Type: {chat_type}\n"
                            f"Use this with `deliver_to=\"{session_id}\"` for scheduled tasks.",
                        )
                        continue

                    if text.strip() == "/start":
                        runner_check = get_runner_fn()
                        if runner_check:
                            await adapter.send_message(
                                chat_id,
                                f"{bot_name} is online and ready. Send me a message to get started.",
                            )
                        else:
                            await adapter.send_message(
                                chat_id,
                                f"Welcome to {bot_name}!\n\n"
                                "The bot needs a Google API key before it can respond.\n\n"
                                "Send the following command to configure it:\n"
                                "`/init YOUR_PASSCODE GOOGLE_API_KEY=your-key-here`\n\n"
                                "Your admin passcode was printed to the server console on first start."
                            )
                        continue

                    # SECURE KEY CAPTURE: intercept before anything reaches the agent
                    from app.secure_config import capture_key, check_pending, capture_friend_key, check_pending_friend

                    if check_pending(session_id):
                        if text:
                            result = capture_key(session_id, text)
                            await adapter.delete_message(chat_id, message_id)
                            await adapter.send_message(chat_id, result["message"])
                        continue

                    if check_pending_friend(session_id):
                        if text:
                            result = capture_friend_key(session_id, text)
                            await adapter.delete_message(chat_id, message_id)
                            await adapter.send_message(chat_id, result["message"])
                        continue

                    # TOTP VERIFICATION: intercept 6-digit codes for pending /init
                    from app.app_utils.config import (
                        has_pending_totp,
                        verify_pending_totp,
                    )

                    if has_pending_totp(session_id):
                        if text:
                            await adapter.delete_message(chat_id, message_id)
                            success, result_msg = verify_pending_totp(
                                session_id, text.strip()
                            )
                            if success and "updated" in result_msg.lower():
                                # Force runner reload to pick up new config
                                _runner = get_runner_fn()  # noqa: F841
                                reload_whitelist() # Also reload whitelist in case env changed
                            await adapter.send_message(chat_id, result_msg)
                        continue

                    # Handle /models command — deterministic, bypasses the LLM entirely.
                    # See ``app.app_utils.models.dispatch_models_command`` for the
                    # canonical command grammar; both pollers route through it so
                    # Slack and Telegram stay in lockstep.
                    if text.strip().startswith("/models"):
                        admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
                        admin_users = {u.strip() for u in admin_users_str.split(",") if u.strip()}
                        caller_ids = {session_id, f"tg_{user_id}"}
                        if admin_users and not (caller_ids & admin_users):
                            await adapter.send_message(
                                chat_id,
                                "Access denied: /models is admin-only.",
                            )
                            continue
                        from app.app_utils.models import dispatch_models_command

                        reply = dispatch_models_command(text)
                        await adapter.send_message(chat_id, reply)
                        continue

                    # Handle /init command
                    if text.strip().startswith("/init"):
                        await adapter.delete_message(chat_id, message_id)
                        result = process_init_fn(text, session_id=session_id)
                        if "updated" in result.lower():
                            reload_whitelist() # Reload whitelist if env updated
                        await adapter.send_message(chat_id, result)
                        continue

                    # Handle /reset command — bypass the agent entirely.
                    # Bare /reset is ambiguous: session reset, process restart,
                    # rollback, and git workspace reset are all valid operations.
                    if text.strip() == "/reset":
                        await adapter.send_message(
                            chat_id,
                            "Which reset do you mean? Reply with one: "
                            "`/reset session`, `call update_self`, "
                            "`rollback previous commit`, or `git workspace reset`.",
                        )
                        continue

                    if text.strip() == "/reset session":
                        runner_check = get_runner_fn()
                        if runner_check:
                            from app.core.agent_executor import _perform_session_refresh

                            # Cancel any in-flight task for this session
                            if (
                                session_id in _active_tasks
                                and not _active_tasks[session_id][0].done()
                            ):
                                _active_tasks[session_id][0].cancel()

                            refresh_msg = await _perform_session_refresh(
                                runner_check, session_user_id, session_id, "fresh"
                            )
                            await adapter.send_message(
                                chat_id,
                                f"Session reset. {bot_name} is ready for a fresh conversation.",
                            )
                        else:
                            await adapter.send_message(
                                chat_id,
                                f"{bot_name} is not configured yet — nothing to reset.",
                            )
                        continue

                    runner = get_runner_fn()
                    if not runner:
                        await adapter.send_message(
                            chat_id,
                            f"{bot_name} is not configured yet.\n\n"
                            "No LLM provider detected. To set up, send:\n"
                            "`/init YOUR_PASSCODE GOOGLE_API_KEY=your-key`\n"
                            "or\n"
                            "`/init YOUR_PASSCODE ANTHROPIC_API_KEY=your-key`\n\n"
                            "For Vertex AI, configure via the setup wizard."
                        )
                        continue

                    is_mentioned = bot_username and (f"@{bot_username}" in text)
                    if is_group and not is_mentioned:
                        logger.info(
                            "Silently adding group message for context to session %s",
                            session_id,
                        )
                        asyncio.create_task(
                            _save_context(
                                runner, session_user_id, session_id, message_content
                            )
                        )
                        continue

                    if mg_id:
                        if mg_id not in _media_group_buffers:
                            _media_group_buffers[mg_id] = []
                        _media_group_buffers[mg_id].extend(message_content.parts)

                        if mg_id in _media_group_timers:
                            _media_group_timers[mg_id].cancel()
                        _media_group_timers[mg_id] = asyncio.create_task(
                            flush_media_group(
                                mg_id,
                                runner,
                                session_user_id,
                                session_id,
                                user_id,
                                chat_id,
                            )
                        )
                        continue

                    # Mid-flight Cancellation Logic
                    if (
                        session_id in _active_tasks
                        and not _active_tasks[session_id][0].done()
                    ):
                        prev_task, prev_msg = _active_tasks[session_id]
                        prev_task.cancel()
                        # Persist the interrupted message as context sequentially so it's not lost
                        asyncio.create_task(
                            _save_context(runner, session_user_id, session_id, prev_msg)
                        )
                        if text.strip().lower() in [
                            "cancel",
                            "stop",
                            "abort",
                            "nevermind",
                        ]:
                            await adapter.send_message(
                                chat_id, "Aborted previous request seamlessly."
                            )
                            continue
                        else:
                            await adapter.send_message(
                                chat_id,
                                "Aborting previous task to prioritize new input...",
                            )

                    # Launch the agent response dynamically in the background mapping to the session
                    task = asyncio.create_task(
                        _process_and_send(
                            runner,
                            session_user_id,
                            session_id,
                            message_content,
                            user_id,
                            chat_id,
                        )
                    )
                    _active_tasks[session_id] = (task, message_content)

            except httpx.ReadTimeout:
                pass
            except asyncio.CancelledError:
                logger.info("Telegram poller shutting down")
                return
            except Exception:
                logger.exception("Telegram poller error, retrying in 5s")
                await asyncio.sleep(5)

            # Check for exit signal after each poll cycle (clean shutdown)
            from app.tools.system import check_exit_signal, consume_exit_signal
            if check_exit_signal():
                if consume_exit_signal():
                    logger.info("Telegram poller exiting for clean shutdown...")
                    return  # Exit coroutine → asyncio.gather completes → main() exits
