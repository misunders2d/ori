"""Slack Socket Mode loop — pre-agent gates + agent dispatch.

Owns the message lifecycle for Slack inbound:
- Socket Mode handler (slack_bolt) — no public webhook needed.
- Pre-agent gates (channel logging, perimeter ACL, blacklist, /reset,
  /init, /models, secure key capture, TOTP verification, group/DM
  mention requirement).
- File-upload ingestion via the adapter's authenticated download.
- Mid-flight task cancellation.
- Heartbeat for the health plugin.

Security parity with the Telegram poller — every gate Telegram has, Slack
has too. Differences are only where Slack's API requires it (Socket Mode
vs long-poll, file URLs require auth, no native typing indicator).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import weakref
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
from google.genai import types

from app.runtime.channel_logger import log_message
from app.runtime.executor import (
    _inject_metadata_header,
    _perform_session_refresh,
    extract_agent_response,
    process_message_for_context,
)
from app.runtime.perimeter import (
    is_allowed,
    is_blacklisted,
    should_notify_admin,
    whitelist_chat,
)
from app.runtime.perimeter import (
    reload as reload_perimeter,
)
from app.runtime.transport import (
    get_adapter,
    parse_notify_from_session_id,
    register_adapter,
)
from app.tools.google_oauth.token_store import resolve_email, save_user_mapping
from app.transports.slack.adapter import SlackAdapter, _scrub_secrets, _update_heartbeat

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """Whether the Slack poller should run.

    Both `SLACK_BOT_TOKEN` (xoxb-) and `SLACK_APP_TOKEN` (xapp-) are required
    for Socket Mode; the bot token alone is insufficient.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not bot_token or not app_token:
        return False
    if not bot_token.startswith("xoxb-"):
        return False
    if not app_token.startswith("xapp-"):
        return False
    return True


async def start_poller(
    get_runner_fn: Callable, process_init_fn: Callable
) -> None:
    """Public entry point — used by run_bot.py via the transports REGISTRY."""
    return await poll_slack(get_runner_fn, process_init_fn)


async def poll_slack(get_runner_fn, process_init_fn):
    """Run a Slack Socket Mode listener using slack_bolt."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app_token = os.environ.get("SLACK_APP_TOKEN", "").strip()

    if not bot_token or not app_token:
        logger.warning("Slack: missing SLACK_BOT_TOKEN or SLACK_APP_TOKEN, poller disabled")
        return
    if not bot_token.startswith("xoxb-"):
        logger.error("Slack: SLACK_BOT_TOKEN should start with 'xoxb-'")
        return
    if not app_token.startswith("xapp-"):
        logger.error("Slack: SLACK_APP_TOKEN should start with 'xapp-'")
        return

    try:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp
    except ImportError:
        logger.error("Slack: slack-bolt not installed (run: uv add slack-bolt)")
        return

    http_client = httpx.AsyncClient(timeout=30)
    adapter = SlackAdapter(http_client, bot_token)
    register_adapter(adapter)

    slack_app = AsyncApp(token=bot_token)

    # Resolve bot user_id once for mention detection.
    _bot_user_id: str | None = None
    try:
        auth_resp = await http_client.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        auth_data = auth_resp.json()
        if auth_data.get("ok"):
            _bot_user_id = auth_data.get("user_id")
            logger.info("Slack: bot_user_id = %s", _bot_user_id)
    except Exception:
        logger.warning("Slack: could not resolve bot user_id (mention detection limited)")

    _active_tasks: dict[str, tuple[asyncio.Task, types.Content]] = {}
    _session_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

    async def _get_lock(sid: str) -> asyncio.Lock:
        lock = _session_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            _session_locks[sid] = lock
        return lock

    async def _process_and_send(
        _runner, _session_user_id, _session_id, _message_content,
        _user_id, _channel_id, _thread_ts="",
    ):
        async with await _get_lock(_session_id):
            thinking_ts = await adapter.send_thinking_indicator(
                _channel_id, thread_ts=_thread_ts
            )
            try:
                response = await extract_agent_response(
                    _runner, _session_user_id, _session_id, _message_content, _user_id,
                )
                if thinking_ts:
                    await adapter.delete_message(_channel_id, thinking_ts)
                if response.text:
                    clean_text = re.sub(r"^Metadata:.*?\n", "", response.text).lstrip()
                    scrubbed = _scrub_secrets(clean_text or response.text)
                    await adapter.send_message(_channel_id, scrubbed, thread_ts=_thread_ts)
                for media_item in response.media_items:
                    await adapter.send_media(
                        _channel_id,
                        media_item["data"],
                        media_item["mime_type"],
                        thread_ts=_thread_ts,
                    )
            except asyncio.CancelledError:
                if thinking_ts:
                    await adapter.delete_message(_channel_id, thinking_ts)
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

    # Acknowledge non-message events to suppress slack_bolt "unhandled request" warnings.
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
        return None

    for _evt in _SILENCED_EVENTS:
        slack_app.event(_evt)(_noop_handler)

    @slack_app.command("/reset")
    async def handle_reset_command(ack, body, respond):
        await ack()
        channel_id = body.get("channel_id", "")
        session_id = adapter.make_session_id(channel_id)
        bot_name = os.environ.get("BOT_NAME", "Ori")
        runner_check = get_runner_fn()
        if runner_check:
            if session_id in _active_tasks and not _active_tasks[session_id][0].done():
                _active_tasks[session_id][0].cancel()
            await _perform_session_refresh(runner_check, session_id, session_id, "fresh")
            await respond(f"Session reset. {bot_name} is ready for a fresh conversation.")
        else:
            await respond(f"{bot_name} is not configured yet — nothing to reset.")

    @slack_app.event("message")
    async def handle_message(event, say):
        _update_heartbeat()

        _files = event.get("files") or []
        logger.info(
            "Slack message event: subtype=%r, files_count=%d, has_text=%s, "
            "channel_type=%s, thread=%s, first_file_mime=%r",
            event.get("subtype"),
            len(_files),
            bool(event.get("text")),
            event.get("channel_type"),
            bool(event.get("thread_ts")),
            (_files[0].get("mimetype") if _files else None),
        )

        # Ignore bot edits, message_changed, etc. but accept file_share.
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            logger.info("Slack: dropping event with subtype=%r", subtype)
            return

        text = event.get("text", "")
        channel_id = event.get("channel")
        user_raw = event.get("user", "")
        ts = event.get("ts", "")
        channel_type = event.get("channel_type", "channel")

        if not channel_id or not user_raw:
            return

        is_dm = channel_type == "im"
        is_mentioned = bool(_bot_user_id and f"<@{_bot_user_id}>" in text)

        session_id = adapter.make_session_id(channel_id)
        sl_id = adapter.make_user_id(user_raw)
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
                display_name = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or user_raw
                )
                user_email = profile.get("email", "")
                if user_email:
                    save_user_mapping(sl_id, user_email)
        except Exception:
            pass

        if not user_email:
            user_email = resolve_email(sl_id)

        # Email is the canonical user_id when available — admin checks compare to it.
        user_id = user_email if user_email else sl_id

        try:
            msg_timestamp = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            msg_timestamp = datetime.now(tz=timezone.utc)

        # --- ACCESS CONTROL GATE ---
        user_authorized = is_allowed(user_id)
        channel_authorized = (not is_dm) and is_allowed(session_id)

        if not (user_authorized or channel_authorized):
            if is_blacklisted(user_id) or is_blacklisted(session_id):
                return

            if should_notify_admin(user_id):
                admin_ids_str = (
                    os.environ.get("ADMIN_USER_IDS", "")
                    or os.environ.get("ALLOWED_USER_IDS", "")
                )
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
            if text.lower().startswith("blacklist "):
                target_id = text.split(" ", 1)[1].strip()
                from app.runtime.perimeter import blacklist_chat as _bl

                _bl(target_id)
                await say(f"Added `{target_id}` to blacklist.")
                return

        bot_name = os.environ.get("BOT_NAME", "Ori")

        # /start equivalent (plain text — Slack reserves real slash commands)
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

        # --- SECURE KEY CAPTURE ---
        from app.runtime.secure_capture import (
            capture_friend_key,
            capture_key,
            check_pending,
            check_pending_friend,
        )

        if check_pending(session_id):
            if text:
                result = capture_key(session_id, text)
                # Attempt to delete the message containing the secret.
                try:
                    await http_client.post(
                        "https://slack.com/api/chat.delete",
                        json={"channel": channel_id, "ts": ts},
                        headers={
                            "Authorization": f"Bearer {bot_token}",
                            "Content-Type": "application/json",
                        },
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
                        headers={
                            "Authorization": f"Bearer {bot_token}",
                            "Content-Type": "application/json",
                        },
                    )
                except Exception:
                    pass
                await say(result["message"])
            return

        # --- TOTP VERIFICATION ---
        from app.util.config import has_pending_totp, verify_pending_totp

        if has_pending_totp(session_id):
            if text:
                try:
                    await http_client.post(
                        "https://slack.com/api/chat.delete",
                        json={"channel": channel_id, "ts": ts},
                        headers={
                            "Authorization": f"Bearer {bot_token}",
                            "Content-Type": "application/json",
                        },
                    )
                except Exception:
                    pass
                success, result_msg = verify_pending_totp(session_id, text.strip())
                if success and "updated" in result_msg.lower():
                    get_runner_fn()
                    reload_perimeter()
                await say(result_msg)
            return

        # --- /models (admin-only model assignments dump/reset) ---
        if text.strip().startswith("/models"):
            admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
            admin_users = {u.strip() for u in admin_users_str.split(",") if u.strip()}
            caller_ids = {session_id, sl_id}
            if user_email:
                caller_ids.add(user_email)
            if admin_users and not (caller_ids & admin_users):
                await say("Access denied: /models is admin-only.")
                return

            from app.util.models import (
                VALID_COMPONENTS,
                format_model_assignments,
                reset_all_models,
                reset_model,
            )

            parts = text.strip().split()
            if len(parts) == 1:
                await say("```\n" + format_model_assignments(markdown=False) + "\n```")
            elif len(parts) >= 2 and parts[1].lower() == "default":
                if len(parts) == 2:
                    cleared = reset_all_models()
                    msg = (
                        f"Reset {len(cleared)} model override(s) to defaults."
                        if cleared
                        else "No overrides to reset — everything is already on defaults."
                    )
                    await say(
                        msg + "\n\n```\n" + format_model_assignments(markdown=False) + "\n```"
                    )
                else:
                    component = parts[2]
                    if component not in VALID_COMPONENTS:
                        await say(
                            f"Unknown component '{component}'. "
                            f"Valid: {', '.join(sorted(VALID_COMPONENTS))}"
                        )
                    else:
                        cleared = reset_model(component)
                        msg = (
                            f"Reset {component} to default."
                            if cleared
                            else f"{component} was already on its default — nothing to clear."
                        )
                        await say(msg)
            else:
                await say(
                    "Usage:\n"
                    "  `/models` — list all agent model assignments\n"
                    "  `/models default` — reset ALL agents to defaults\n"
                    "  `/models default <Component>` — reset one component"
                )
            return

        # --- /init (LLM-offline credential injection) ---
        if text.strip().startswith("/init"):
            try:
                await http_client.post(
                    "https://slack.com/api/chat.delete",
                    json={"channel": channel_id, "ts": ts},
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json",
                    },
                )
            except Exception:
                pass
            result = process_init_fn(text, session_id=session_id)
            if "updated" in result.lower():
                reload_perimeter()
            await say(result)
            return

        # Plain "reset" / "reset session" — Slack /slash commands are intercepted
        # by Slack and don't arrive as messages, so we match plain text variants.
        _clean_text = text.strip()
        if _bot_user_id:
            _clean_text = _clean_text.replace(f"<@{_bot_user_id}>", "").strip()
        if _clean_text.lower() in ("reset", "reset session"):
            runner_check = get_runner_fn()
            if runner_check:
                if session_id in _active_tasks and not _active_tasks[session_id][0].done():
                    _active_tasks[session_id][0].cancel()
                await _perform_session_refresh(runner_check, session_id, session_id, "fresh")
                await say(f"Session reset. {bot_name} is ready for a fresh conversation.")
            else:
                await say(f"{bot_name} is not configured yet — nothing to reset.")
            return

        # --- FILE INGESTION ---
        file_parts: list = []
        file_info_text = ""
        for file_obj in event.get("files", []):
            url_private = file_obj.get("url_private")
            if not url_private:
                continue
            if not url_private.startswith("https://files.slack.com/"):
                logger.warning("Ignoring non-Slack file URL: %s", url_private)
                continue
            file_size = file_obj.get("size", 0)
            if file_size > 20 * 1024 * 1024:
                logger.warning(
                    "Skipping oversized Slack file (%d bytes): %s",
                    file_size, file_obj.get("name"),
                )
                continue
            file_data = await adapter.download_file(
                url_private,
                mime_hint=file_obj.get("mimetype", "") or "",
                filename_hint=file_obj.get("name", "") or "",
            )
            if not file_data:
                continue
            blob_bytes, mime_type, filename = file_data
            from app.util.file_convert import prepare_for_llm, save_upload

            saved_path = save_upload(blob_bytes, filename)
            prepared = prepare_for_llm(blob_bytes, mime_type, filename, saved_path)
            file_parts.append(types.Part.from_text(text=prepared.text))
            if prepared.inline_blob:
                _blob_bytes, _blob_mime = prepared.inline_blob
                file_parts.append(
                    types.Part(inline_data=types.Blob(data=_blob_bytes, mime_type=_blob_mime))
                )
            file_info_text += f" [{file_obj.get('filetype', 'file').upper()}: {filename}]"

        if not text and not file_parts:
            return

        # --- BUILD MESSAGE CONTENT ---
        raw_text = (
            f"Message from {display_name} ({user_id}): {text} {file_info_text}".strip()
        )
        enriched_text = _inject_metadata_header(raw_text, msg_timestamp, "slack")

        message_content = types.Content(role="user", parts=[])
        if enriched_text:
            message_content.parts.append(types.Part.from_text(text=enriched_text))
        message_content.parts.extend(file_parts)

        log_message(session_id, user_id, display_name, text)

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

        # In groups/channels: silently absorb context if not mentioned.
        if not is_dm and not is_mentioned:
            asyncio.create_task(_save_context(runner, session_id, session_id, message_content))
            return

        # Mid-flight cancellation
        if session_id in _active_tasks and not _active_tasks[session_id][0].done():
            prev_task, prev_msg = _active_tasks[session_id]
            prev_task.cancel()
            asyncio.create_task(_save_context(runner, session_id, session_id, prev_msg))
            if text.strip().lower() in ("cancel", "stop", "abort", "nevermind"):
                await say("Aborted previous request.")
                return
            await say("Aborting previous task to prioritize new input...")

        # Reply in-thread to keep multi-turn conversations contained.
        reply_thread_ts = event.get("thread_ts") or ts

        task = asyncio.create_task(
            _process_and_send(
                runner, session_id, session_id, message_content,
                user_id, channel_id, reply_thread_ts,
            )
        )
        _active_tasks[session_id] = (task, message_content)

    print("  Slack: Socket Mode active — listening for messages...")
    handler = AsyncSocketModeHandler(slack_app, app_token)

    try:
        await handler.start_async()
    except asyncio.CancelledError:
        logger.info("Slack poller shutting down")
        await handler.close_async()
        await http_client.aclose()
    except Exception:
        logger.exception("Slack Socket Mode handler crashed")
