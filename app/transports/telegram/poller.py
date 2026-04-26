"""Telegram long-poll loop — pre-agent gates + agent dispatch.

Owns the message lifecycle for Telegram inbound:
- Long-poll getUpdates with offset persistence.
- Pre-agent gates (channel logging, perimeter ACL, blacklist, /start,
  /init, /reset, /models, secure key capture, TOTP verification, group
  mention requirement).
- Media-group coalescing.
- Mid-flight task cancellation.
- Heartbeat for the health plugin.

The poller's module-level imports include `app.runtime.executor` (which
is built in Phase G). Until Phase G lands, this file will fail to import
— that's expected mid-rebuild. Phase E only verifies the adapter, not
the poller.
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from collections.abc import Callable
from datetime import datetime

import httpx
from google.genai import types

from app.runtime.channel_logger import log_message
from app.runtime.executor import (
    _inject_metadata_header,
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
from app.runtime.transport import register_adapter
from app.transports.telegram.adapter import (
    TELEGRAM_API,
    TelegramAdapter,
    _update_heartbeat,
)

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """Whether the telegram poller should run."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return False
    # If a webhook secret is set, the bot routes via webhooks instead of polling.
    if os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
        return False
    return True


async def start_poller(
    get_runner_fn: Callable, process_init_fn: Callable
) -> None:
    """Public entry point — used by run_bot.py via the transports REGISTRY."""
    return await poll_telegram(get_runner_fn, process_init_fn)


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
        adapter = TelegramAdapter(client, token)
        register_adapter(adapter)

        _active_tasks: dict[str, tuple[asyncio.Task, types.Content]] = {}
        _media_group_buffers: dict[str, list[types.Part]] = {}
        _media_group_timers: dict[str, asyncio.Task] = {}
        _session_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

        async def _get_lock(sid: str) -> asyncio.Lock:
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
                    if response.text:
                        await adapter.send_message(_chat_id, response.text)
                    for media_item in response.media_items:
                        await adapter.send_media(
                            _chat_id, media_item["data"], media_item["mime_type"],
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

        async def _save_context(_runner, _session_user_id, _session_id, _message_content):
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
                    _chat_id, "Aborting previous task to prioritize new grouped media...",
                )
            task = asyncio.create_task(
                _process_and_send(
                    _runner, _session_user_id, _session_id, combined_content, _user_id, _chat_id
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
                    is_group = chat_type in ("group", "supergroup")
                    message_id = msg["message_id"]
                    from_user = msg.get("from", {})
                    unix_ts = msg.get("date", int(datetime.now().timestamp()))
                    msg_timestamp = datetime.utcfromtimestamp(unix_ts)
                    display_name = from_user.get("first_name", "Unknown")
                    if from_user.get("last_name"):
                        display_name += f" {from_user['last_name']}"
                    user_id = adapter.make_user_id(from_user.get("id", "unknown"))
                    session_id = adapter.make_session_id(chat_id)
                    session_user_id = session_id  # group identity isolation

                    # Channel logging — silent log, no agent invocation.
                    if chat_type == "channel":
                        if is_allowed(session_id):
                            log_message(session_id, user_id, display_name, text)
                        continue

                    # Access control gate.
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
                            admin_ids_str = os.environ.get("ADMIN_USER_IDS", "")
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

                    # File handling.
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
                        file_info_text = f"[Audio: {msg['audio'].get('title', 'unknown')}]"
                    elif "video" in msg:
                        file_id = msg["video"]["file_id"]
                        file_info_text = f"[Video: {msg['video'].get('file_name', 'unknown')}]"
                    elif "video_note" in msg:
                        file_id = msg["video_note"]["file_id"]
                        file_info_text = "[Video Message]"

                    if not text and not file_id:
                        continue

                    mg_id = msg.get("media_group_id")
                    if mg_id and not text:
                        enriched_text = ""
                    else:
                        raw_text = (
                            f"Message from {display_name} ({user_id}): {text} {file_info_text}".strip()
                        )
                        enriched_text = _inject_metadata_header(raw_text, msg_timestamp, "telegram")

                    message_content = types.Content(role="user", parts=[])
                    if enriched_text:
                        message_content.parts.append(types.Part.from_text(text=enriched_text))
                    if file_id:
                        file_data = await adapter.download_file(file_id)
                        if file_data:
                            blob_bytes, mime_type, _ = file_data
                            message_content.parts.append(
                                types.Part(
                                    inline_data=types.Blob(data=blob_bytes, mime_type=mime_type)
                                )
                            )

                    # Whitelist/blacklist shortcuts (admin/owner UX).
                    if is_allowed(user_id):
                        if text.lower().startswith("whitelist "):
                            target_id = text.split(" ")[1].strip()
                            whitelist_chat(target_id)
                            await adapter.send_message(chat_id, f"✅ Added `{target_id}` to whitelist.")
                            continue
                        elif text.lower().startswith("blacklist "):
                            target_id = text.split(" ")[1].strip()
                            from app.runtime.perimeter import blacklist_chat as _bl
                            _bl(target_id)
                            await adapter.send_message(chat_id, f"🌑 Added `{target_id}` to blacklist.")
                            continue

                    bot_name = os.environ.get("BOT_NAME", "Ori")

                    # /start command.
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

                    # Secure key capture — intercept BEFORE the agent sees the message.
                    from app.runtime.secure_capture import (
                        capture_friend_key,
                        capture_key,
                        check_pending,
                        check_pending_friend,
                    )
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

                    # TOTP verification for pending /init.
                    from app.util.config import has_pending_totp, verify_pending_totp
                    if has_pending_totp(session_id):
                        if text:
                            await adapter.delete_message(chat_id, message_id)
                            success, result_msg = verify_pending_totp(session_id, text.strip())
                            if success and "updated" in result_msg.lower():
                                _runner = get_runner_fn()
                                reload_perimeter()
                            await adapter.send_message(chat_id, result_msg)
                        continue

                    # /models — admin-only model assignments dump/reset.
                    # NOTE: the helper functions referenced here will land with
                    # the model-admin tools in Phase F. Until then the command
                    # raises ImportError on use; everything else still works.
                    if text.strip().startswith("/models"):
                        admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
                        admin_users = {u.strip() for u in admin_users_str.split(",") if u.strip()}
                        caller_ids = {session_id, f"tg_{user_id}"}
                        if admin_users and not (caller_ids & admin_users):
                            await adapter.send_message(chat_id, "Access denied: /models is admin-only.")
                            continue
                        try:
                            from app.util.models import (  # type: ignore[attr-defined]
                                format_model_assignments,
                                list_components,
                                reset_all_models,
                                reset_model,
                            )
                        except ImportError:
                            await adapter.send_message(
                                chat_id, "/models is not yet wired in this build.",
                            )
                            continue
                        # Fetch session state so we can show state-level overrides
                        # (set_agent_model writes there). Without this, /models
                        # only sees env vars and misleads when state has been
                        # mutated at runtime.
                        state_overrides: dict[str, str] = {}
                        runner_obj = get_runner_fn()
                        if runner_obj is not None:
                            try:
                                sess = await runner_obj.session_service.get_session(
                                    app_name=runner_obj.app_name,
                                    user_id=session_id,
                                    session_id=session_id,
                                )
                                if sess and sess.state:
                                    state_dict = sess.state if isinstance(sess.state, dict) else dict(sess.state)
                                    state_overrides = state_dict.get("model") or {}
                            except Exception:
                                pass  # state read is best-effort
                        parts_ = text.strip().split()
                        if len(parts_) == 1:
                            await adapter.send_message(
                                chat_id, "```\n" + format_model_assignments(markdown=False, state_overrides=state_overrides) + "\n```",
                            )
                        elif len(parts_) >= 2 and parts_[1].lower() == "default":
                            valid = set(list_components())
                            if len(parts_) == 2:
                                cleared = reset_all_models()
                                msg = (
                                    f"Reset {len(cleared)} model override(s) to defaults."
                                    if cleared else "No overrides to reset — already on defaults."
                                )
                                await adapter.send_message(
                                    chat_id, msg + "\n\n```\n" + format_model_assignments(markdown=False, state_overrides=state_overrides) + "\n```",
                                )
                            else:
                                comp = parts_[2]
                                if comp not in valid:
                                    await adapter.send_message(
                                        chat_id,
                                        f"Unknown component '{comp}'. Valid: {', '.join(sorted(valid))}",
                                    )
                                else:
                                    cleared = reset_model(comp)
                                    msg = (
                                        f"Reset {comp} to default." if cleared
                                        else f"{comp} was already on its default."
                                    )
                                    await adapter.send_message(chat_id, msg)
                        else:
                            await adapter.send_message(
                                chat_id,
                                "Usage:\n"
                                "  `/models` — list all agent model assignments\n"
                                "  `/models default` — reset ALL to defaults\n"
                                "  `/models default <Component>` — reset one to default",
                            )
                        continue

                    # /init — credential injection (LLM-offline recovery).
                    if text.strip().startswith("/init"):
                        await adapter.delete_message(chat_id, message_id)
                        result = process_init_fn(text, session_id=session_id)
                        if "updated" in result.lower():
                            reload_perimeter()
                        await adapter.send_message(chat_id, result)
                        continue

                    # /reset — wipe session.
                    if text.strip() == "/reset":
                        runner_check = get_runner_fn()
                        if runner_check:
                            from app.runtime.executor import _perform_session_refresh
                            if (
                                session_id in _active_tasks
                                and not _active_tasks[session_id][0].done()
                            ):
                                _active_tasks[session_id][0].cancel()
                            await _perform_session_refresh(
                                runner_check, session_user_id, session_id, "fresh"
                            )
                            await adapter.send_message(
                                chat_id, f"Session reset. {bot_name} is ready for a fresh conversation.",
                            )
                        else:
                            await adapter.send_message(
                                chat_id, f"{bot_name} is not configured yet — nothing to reset.",
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

                    # Group mention requirement — group msgs without bot mention save context only.
                    is_mentioned = bot_username and (f"@{bot_username}" in text)
                    if is_group and not is_mentioned:
                        logger.info(
                            "Silently adding group message for context to session %s", session_id,
                        )
                        asyncio.create_task(
                            _save_context(runner, session_user_id, session_id, message_content)
                        )
                        continue

                    # Media-group coalesce.
                    if mg_id:
                        _media_group_buffers.setdefault(mg_id, []).extend(message_content.parts)
                        if mg_id in _media_group_timers:
                            _media_group_timers[mg_id].cancel()
                        _media_group_timers[mg_id] = asyncio.create_task(
                            flush_media_group(
                                mg_id, runner, session_user_id, session_id, user_id, chat_id,
                            )
                        )
                        continue

                    # Mid-flight cancellation.
                    if (
                        session_id in _active_tasks
                        and not _active_tasks[session_id][0].done()
                    ):
                        prev_task, prev_msg = _active_tasks[session_id]
                        prev_task.cancel()
                        asyncio.create_task(
                            _save_context(runner, session_user_id, session_id, prev_msg)
                        )
                        if text.strip().lower() in ("cancel", "stop", "abort", "nevermind"):
                            await adapter.send_message(chat_id, "Aborted previous request seamlessly.")
                            continue
                        else:
                            await adapter.send_message(
                                chat_id, "Aborting previous task to prioritize new input...",
                            )

                    task = asyncio.create_task(
                        _process_and_send(
                            runner, session_user_id, session_id, message_content, user_id, chat_id,
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

            # Clean shutdown signal.
            from app.tools.system import check_exit_signal, consume_exit_signal
            if check_exit_signal():
                if consume_exit_signal():
                    logger.info("Telegram poller exiting for clean shutdown...")
                    return
