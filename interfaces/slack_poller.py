import asyncio
import logging
import os
import re
import weakref
from datetime import datetime, timezone

import httpx
from google.genai import types

from app.core.agent_executor import (
    _inject_metadata_header,
    extract_agent_response,
    process_message_for_context,
)
from app.core.transport import register_adapter, get_adapter, parse_notify_from_session_id
from app.core.transport_slack import SlackAdapter
from app.core.whitelist import (
    is_allowed,
    is_blacklisted,
    should_notify_admin,
    whitelist_chat,
    reload as reload_whitelist,
)
from app.core.channel_logger import log_message

logger = logging.getLogger(__name__)

HEARTBEAT_FILE = os.path.abspath("./data/.slack_heartbeat")

# Keys whose env values are sensitive secrets
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


def _update_heartbeat():
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(datetime.now().isoformat())
    except Exception:
        pass


async def poll_slack(get_runner_fn, process_init_fn):
    """Run a Slack Socket Mode listener using slack-bolt, processing messages through the agent."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")

    if not bot_token:
        logger.warning("Slack: No SLACK_BOT_TOKEN configured, poller disabled")
        return
    if not app_token:
        logger.warning("Slack: No SLACK_APP_TOKEN configured (required for Socket Mode), poller disabled")
        return

    # Validate token prefixes
    if not bot_token.startswith("xoxb-"):
        logger.error("Slack: SLACK_BOT_TOKEN should start with 'xoxb-'. Check your configuration.")
        return
    if not app_token.startswith("xapp-"):
        logger.error("Slack: SLACK_APP_TOKEN should start with 'xapp-'. Check your configuration.")
        return

    try:
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    except ImportError:
        logger.error("Slack: slack-bolt not installed. Run: uv add slack-bolt")
        return

    # Create the shared httpx client and register the transport adapter
    http_client = httpx.AsyncClient(timeout=30)
    adapter = SlackAdapter(http_client, bot_token)
    register_adapter(adapter)

    # Bolt app handles Socket Mode auth and event routing
    slack_app = AsyncApp(token=bot_token)

    # Resolve bot's own user ID for mention detection
    _bot_user_id = None
    try:
        auth_resp = await http_client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        auth_data = auth_resp.json()
        if auth_data.get("ok"):
            _bot_user_id = auth_data.get("user_id")
    except Exception:
        pass

    # Session management
    _active_tasks: dict[str, tuple[asyncio.Task, types.Content]] = {}
    _session_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

    async def _get_lock(sid: str) -> asyncio.Lock:
        lock = _session_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[sid] = lock
        return lock

    async def _process_and_send(
        _runner, _session_user_id, _session_id, _message_content, _user_id, _channel_id, _thread_ts=""
    ):
        async with await _get_lock(_session_id):
            try:
                response = await extract_agent_response(
                    _runner,
                    _session_user_id,
                    _session_id,
                    _message_content,
                    _user_id,
                )
                if response.text:
                    scrubbed = _scrub_secrets(response.text)
                    await adapter.send_message(_channel_id, scrubbed, thread_ts=_thread_ts)
                for media_item in response.media_items:
                    await adapter.send_media(
                        _channel_id,
                        media_item["data"],
                        media_item["mime_type"],
                        thread_ts=_thread_ts,
                    )
            except asyncio.CancelledError:
                pass
            finally:
                if (
                    _session_id in _active_tasks
                    and _active_tasks[_session_id][0] == asyncio.current_task()
                ):
                    del _active_tasks[_session_id]

    async def _save_context(_runner, _session_user_id, _session_id, _message_content):
        async with await _get_lock(_session_id):
            try:
                await process_message_for_context(
                    _runner, _session_user_id, _session_id, _message_content
                )
            except Exception:
                logger.exception("Failed to save context for session %s", _session_id)

    # Acknowledge all non-message events to suppress "unhandled request" warnings.
    # Slack sends ~129 event types; we only actively process "message".
    _SILENCED_EVENTS = [
        "app_mention", "app_home_opened",
        "reaction_added", "reaction_removed",
        "pin_added", "pin_removed",
        "file_shared", "file_created", "file_change", "file_deleted",
        "file_public", "file_unshared", "file_comment_deleted",
        "channel_archive", "channel_created", "channel_deleted",
        "channel_rename", "channel_joined", "channel_left",
        "channel_unarchive", "channel_shared", "channel_unshared",
        "channel_id_changed", "channel_history_changed",
        "group_archive", "group_close", "group_deleted",
        "group_joined", "group_left", "group_open",
        "group_rename", "group_unarchive",
        "im_close", "im_created", "im_open",
        "member_joined_channel", "member_left_channel",
        "team_join", "user_change", "user_typing",
        "emoji_changed", "link_shared",
        "star_added", "star_removed",
        "dnd_updated", "dnd_updated_user",
        "subteam_created", "subteam_updated",
        "subteam_members_changed", "subteam_self_added", "subteam_self_removed",
        "tokens_revoked",
        "message_metadata_posted", "message_metadata_updated", "message_metadata_deleted",
    ]

    async def _noop_handler(event, say):
        pass

    for _evt in _SILENCED_EVENTS:
        slack_app.event(_evt)(_noop_handler)

    @slack_app.event("message")
    async def handle_message(event, say):
        _update_heartbeat()

        # Ignore bot messages, message_changed, etc.
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return

        text = event.get("text", "")
        channel_id = event.get("channel")
        user_raw = event.get("user", "")
        ts = event.get("ts", "")
        channel_type = event.get("channel_type", "channel")

        if not channel_id or not user_raw:
            return

        # Determine if this is a direct message or a group/channel
        is_dm = channel_type == "im"
        is_mentioned = _bot_user_id and f"<@{_bot_user_id}>" in text

        # Build canonical IDs (session is channel-scoped, user_id upgraded to email below)
        session_id = adapter.make_session_id(channel_id)
        session_user_id = session_id

        # Extract display name and email (best-effort from Slack Web API)
        display_name = user_raw
        user_email = ""
        try:
            resp = await http_client.get(
                "https://slack.com/api/users.info",
                params={"user": user_raw},
                headers={"Authorization": f"Bearer {bot_token}"},
            )
            user_data = resp.json()
            if user_data.get("ok"):
                profile = user_data["user"].get("profile", {})
                display_name = profile.get("display_name") or profile.get("real_name") or user_raw
                user_email = profile.get("email", "")
        except Exception:
            pass

        # Use email as canonical user_id if available, fall back to sl_ prefix
        user_id = user_email if user_email else adapter.make_user_id(user_raw)

        # Extract timestamp
        try:
            msg_timestamp = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            msg_timestamp = datetime.now(tz=timezone.utc)

        # --- ACCESS CONTROL GATE ---
        user_authorized = is_allowed(user_id)
        channel_authorized = not is_dm and is_allowed(session_id)

        if not (user_authorized or channel_authorized):
            if is_blacklisted(user_id) or is_blacklisted(session_id):
                return

            if should_notify_admin(user_id):
                admin_ids_str = os.environ.get("ADMIN_USER_IDS", "") or os.environ.get("ALLOWED_USER_IDS", "")
                admin_ids = [u.strip() for u in admin_ids_str.split(",") if u.strip()]
                for admin_id in admin_ids:
                    notify_info = parse_notify_from_session_id(admin_id)
                    if notify_info:
                        target_adapter = get_adapter(notify_info["type"])
                        if target_adapter:
                            await target_adapter.send_message(
                                notify_info["chat_id"],
                                f"*Unauthorized Slack Access Attempt*\n"
                                f"*User:* {display_name} ({user_id})\n"
                                f"*Channel:* {channel_type} ({session_id})\n"
                                f"*Message:* {text}\n\n"
                                f"To allow, reply with: `Whitelist {user_id}`\n"
                                f"To block, reply with: `Blacklist {user_id}`",
                            )
            return

        # --- WHITELIST / BLACKLIST SHORTCUTS ---
        if is_allowed(user_id):
            if text.lower().startswith("whitelist "):
                target_id = text.split(" ", 1)[1].strip()
                whitelist_chat(target_id)
                await say(f"Added `{target_id}` to whitelist.")
                return
            elif text.lower().startswith("blacklist "):
                target_id = text.split(" ", 1)[1].strip()
                from app.core.whitelist import blacklist_chat as _bl
                _bl(target_id)
                await say(f"Added `{target_id}` to blacklist.")
                return

        # --- COMMAND HANDLING ---
        bot_name = os.environ.get("BOT_NAME", "Ori")

        # /start equivalent — just "start" or a Slack slash command
        if text.strip().lower() in ("/start", "start"):
            runner_check = get_runner_fn()
            if runner_check:
                await say(f"{bot_name} is online and ready. Send me a message to get started.")
            else:
                await say(
                    f"Welcome to {bot_name}!\n\n"
                    "The bot needs an LLM API key before it can respond.\n\n"
                    "Send: `/init YOUR_PASSCODE GOOGLE_API_KEY=your-key-here`"
                )
            return

        # SECURE KEY CAPTURE: intercept before anything reaches the agent
        from app.secure_config import capture_key, check_pending, capture_friend_key, check_pending_friend

        if check_pending(session_id):
            if text:
                result = capture_key(session_id, text)
                # Try to delete the message containing the secret
                try:
                    await http_client.post(
                        "https://slack.com/api/chat.delete",
                        json={"channel": channel_id, "ts": ts},
                        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                    )
                except Exception:
                    pass
                await say(result["message"])
            return

        if check_pending_friend(session_id):
            if text:
                result = capture_friend_key(session_id, text)
                try:
                    await http_client.post(
                        "https://slack.com/api/chat.delete",
                        json={"channel": channel_id, "ts": ts},
                        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                    )
                except Exception:
                    pass
                await say(result["message"])
            return

        # TOTP VERIFICATION
        from app.app_utils.config import has_pending_totp, verify_pending_totp

        if has_pending_totp(session_id):
            if text:
                try:
                    await http_client.post(
                        "https://slack.com/api/chat.delete",
                        json={"channel": channel_id, "ts": ts},
                        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                    )
                except Exception:
                    pass
                success, result_msg = verify_pending_totp(session_id, text.strip())
                if success and "updated" in result_msg.lower():
                    get_runner_fn()
                    reload_whitelist()
                await say(result_msg)
            return

        # /init command
        if text.strip().startswith("/init"):
            try:
                await http_client.post(
                    "https://slack.com/api/chat.delete",
                    json={"channel": channel_id, "ts": ts},
                    headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                )
            except Exception:
                pass
            result = process_init_fn(text, session_id=session_id)
            if "updated" in result.lower():
                reload_whitelist()
            await say(result)
            return

        # /reset command
        if text.strip().lower() in ("/reset", "reset session"):
            runner_check = get_runner_fn()
            if runner_check:
                from app.core.agent_executor import _perform_session_refresh

                if session_id in _active_tasks and not _active_tasks[session_id][0].done():
                    _active_tasks[session_id][0].cancel()

                await _perform_session_refresh(runner_check, session_user_id, session_id, "fresh")
                await say(f"Session reset. {bot_name} is ready for a fresh conversation.")
            else:
                await say(f"{bot_name} is not configured yet - nothing to reset.")
            return

        # --- FILE HANDLING ---
        file_parts = []
        file_info_text = ""
        for file_obj in event.get("files", []):
            url_private = file_obj.get("url_private")
            if not url_private:
                continue

            # Validate URL is from Slack
            if not url_private.startswith("https://files.slack.com/"):
                logger.warning("Ignoring non-Slack file URL: %s", url_private)
                continue

            # Enforce file size limit (20MB)
            file_size = file_obj.get("size", 0)
            if file_size > 20 * 1024 * 1024:
                logger.warning("Skipping oversized file (%d bytes): %s", file_size, file_obj.get("name"))
                continue

            file_data = await adapter.download_file(url_private)
            if file_data:
                blob_bytes, mime_type, filename = file_data
                file_parts.append(
                    types.Part(
                        inline_data=types.Blob(data=blob_bytes, mime_type=mime_type)
                    )
                )
                file_info_text += f" [{file_obj.get('filetype', 'file').upper()}: {filename}]"

        # Skip empty messages
        if not text and not file_parts:
            return

        # --- BUILD MESSAGE CONTENT ---
        raw_text = f"Message from {display_name} ({user_id}): {text} {file_info_text}".strip()
        enriched_text = _inject_metadata_header(raw_text, msg_timestamp, "slack")

        message_content = types.Content(role="user", parts=[])
        if enriched_text:
            message_content.parts.append(types.Part.from_text(text=enriched_text))
        message_content.parts.extend(file_parts)

        # --- CHANNEL LOGGING ---
        log_message(session_id, user_id, display_name, text)

        # --- AGENT EXECUTION ---
        runner = get_runner_fn()
        if not runner:
            await say(
                f"{bot_name} is not configured yet.\n\n"
                "No LLM provider detected. To set up, send:\n"
                "`/init YOUR_PASSCODE GOOGLE_API_KEY=your-key`\n"
                "or\n"
                "`/init YOUR_PASSCODE ANTHROPIC_API_KEY=your-key`"
            )
            return

        # In groups/channels: silently absorb context if not mentioned
        if not is_dm and not is_mentioned:
            logger.info("Silently adding group message for context to session %s", session_id)
            asyncio.create_task(
                _save_context(runner, session_user_id, session_id, message_content)
            )
            return

        # Mid-flight cancellation
        if session_id in _active_tasks and not _active_tasks[session_id][0].done():
            prev_task, prev_msg = _active_tasks[session_id]
            prev_task.cancel()
            asyncio.create_task(
                _save_context(runner, session_user_id, session_id, prev_msg)
            )
            if text.strip().lower() in ("cancel", "stop", "abort", "nevermind"):
                await say("Aborted previous request.")
                return
            else:
                await say("Aborting previous task to prioritize new input...")

        # Use thread_ts if this message is already in a thread, otherwise reply in a thread to this message
        reply_thread_ts = event.get("thread_ts") or ts

        task = asyncio.create_task(
            _process_and_send(
                runner,
                session_user_id,
                session_id,
                message_content,
                user_id,
                channel_id,
                reply_thread_ts,
            )
        )
        _active_tasks[session_id] = (task, message_content)

    # --- START SOCKET MODE ---
    print("  Slack: Socket Mode active - listening for messages...")
    handler = AsyncSocketModeHandler(slack_app, app_token)

    try:
        await handler.start_async()
    except asyncio.CancelledError:
        logger.info("Slack poller shutting down")
        await handler.close_async()
        await http_client.aclose()
    except Exception:
        logger.exception("Slack Socket Mode handler crashed")
        await http_client.aclose()
