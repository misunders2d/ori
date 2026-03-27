import asyncio
import logging
import os
import mimetypes

import httpx
from google import genai
from google.genai import types

from app.session_signals import get_pending_refresh

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_FILE_API = "https://api.telegram.org/file/bot{token}/{path}"

# Session lifecycle: pending reset choices keyed by session_id
_pending_session_reset: dict[str, object] = {}  # session_id -> session object


async def send_typing(client: httpx.AsyncClient, token: str, chat_id: int):
    """Send 'typing...' indicator to a Telegram chat."""
    url = TELEGRAM_API.format(token=token, method="sendChatAction")
    try:
        await client.post(url, json={"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


async def send_message(client: httpx.AsyncClient, token: str, chat_id: int, text: str):
    """Send a message to a Telegram chat."""
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    try:
        resp = await client.post(
            url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        )
        if resp.status_code != 200:
            # Markdown was rejected — retry without it
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            if resp.status_code != 200:
                logger.error("Telegram sendMessage failed: %s", resp.text)
    except Exception:
        logger.exception("Failed to send Telegram message to chat %s", chat_id)


async def delete_message(
    client: httpx.AsyncClient, token: str, chat_id: int, message_id: int
):
    """Delete a message from Telegram (to remove the key from chat history)."""
    url = TELEGRAM_API.format(token=token, method="deleteMessage")
    try:
        await client.post(url, json={"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass  # May fail if bot lacks permissions, non-critical


async def download_telegram_file(client: httpx.AsyncClient, token: str, file_id: str) -> tuple[bytes, str, str] | None:
    """Download a file from Telegram and return (bytes, mime_type, filename)."""
    try:
        # 1. Get file path
        url = TELEGRAM_API.format(token=token, method="getFile")
        resp = await client.get(url, params={"file_id": file_id})
        data = resp.json()
        if not data.get("ok"):
            logger.error("Telegram getFile failed: %s", data)
            return None
        
        file_path = data["result"].get("file_path")
        if not file_path:
            return None
        
        # 2. Download file
        download_url = TELEGRAM_FILE_API.format(token=token, path=file_path)
        file_resp = await client.get(download_url)
        if file_resp.status_code != 200:
            logger.error("Telegram file download failed: %d", file_resp.status_code)
            return None
        
        # 3. Guess mime type and filename
        filename = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(filename)
        if not mime_type:
            mime_type = "application/octet-stream"
            
        return file_resp.content, mime_type, filename
    except Exception:
        logger.exception("Error downloading Telegram file %s", file_id)
        return None


MAX_SESSION_EVENTS = 200

SESSION_LIMIT_PROMPT = (
    "This conversation has grown very long ({event_count} events) and is "
    "slowing down my responses.\n\n"
    "Please choose how to proceed:\n"
    "1. *Fresh start* — wipe the session completely\n"
    "2. *Carry over context* — summarize this session and start a new one "
    "with the summary\n\n"
    "Reply *1* or *2*."
)


async def _summarize_session(session) -> str:
    """Use Gemini to summarize session events into a compact context string."""
    texts = []
    for event in session.events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    role = event.content.role or "unknown"
                    texts.append(f"{role}: {part.text}")
    if not texts:
        return ""
    # Take the last 80 exchanges max to stay within model limits
    conversation = "\n".join(texts[-80:])
    client = genai.Client()
    response = await client.aio.models.generate_content(
        model="gemini-3-flash-preview",
        contents=(
            "Summarize the following conversation into a concise context briefing. "
            "Preserve key facts, decisions, ongoing tasks, and user preferences. "
            "Keep it under 500 words.\n\n" + conversation
        ),
    )
    return response.text or ""


async def _perform_session_refresh(
    runner, user_id, session_id, mode: str, session=None
) -> str:
    """Core logic to wipe or summarize/reset a session."""
    if mode == "summarize":
        if session is None:
            session = await runner.session_service.get_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )

        summary = ""
        if session:
            summary = await _summarize_session(session)

        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

        if summary:
            # Seed the new session with the summary as context
            await extract_agent_response(
                runner,
                user_id,
                session_id,
                f"[CONTEXT FROM PREVIOUS SESSION]\n{summary}\n[END CONTEXT]\n\n"
                "Acknowledge that you have received a summary of our previous "
                "conversation. Briefly confirm what you remember.",
            )
            return "New session started with context carried over."
        else:
            return "Could not generate a summary. Started a fresh session instead."
    else:
        # Fresh wipe
        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        return "Fresh session started."


async def _handle_session_reset(runner, user_id, session_id, choice: str) -> str:
    """Process the user's session reset choice. Returns a status message."""
    session = _pending_session_reset.pop(session_id, None)
    mode = "summarize" if choice.strip() == "2" else "fresh"
    return await _perform_session_refresh(runner, user_id, session_id, mode, session)

async def update_session_state(runner, user_id: str, session_id: str, state_delta: dict):
    """Natively injects state_delta into the ADK Session DB without invoking the graph."""
    import uuid
    from google.adk.events.event import Event, EventActions
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    if not session:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
    await runner.session_service.append_event(
        session=session,
        event=Event(
            id=str(uuid.uuid4()),
            author="__system__",
            content=None,
            actions=EventActions(state_delta=state_delta),
        ),
    )


async def extract_agent_response(
    runner, user_id: str, session_id: str, message: str | types.Content, actual_caller_id: str = None
) -> str:
    """Run the ADK runner and yield agent responses."""
    import time
    import uuid
    try:
        from google.adk.events.event import Event, EventActions
    except ImportError:
        import sys
        Event = sys.modules['google.adk.events.event'].Event
        EventActions = sys.modules['google.adk.events.event'].EventActions

    if actual_caller_id:
        await update_session_state(
            runner=runner,
            session_id=session_id,
            user_id=user_id,
            state_delta={"user_id": actual_caller_id},
        )

    try:
        session = await runner.session_service.get_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            await runner.session_service.create_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
        elif len(session.events) > MAX_SESSION_EVENTS:
            logger.warning(
                "Session %s hit hard limit (%d events), prompting user.",
                session_id,
                len(session.events),
            )
            _pending_session_reset[session_id] = session
            return SESSION_LIMIT_PROMPT.format(event_count=len(session.events))
    except Exception:
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

    MAX_RETRIES = 2
    
    if isinstance(message, str):
        message_arg = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        message_arg = message

    parts = []
    for attempt in range(1 + MAX_RETRIES):
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message_arg,
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            parts.append(part.text)
            break  # success
        except Exception as exc:
            error_msg = str(exc).split("\n")[0] if str(exc) else type(exc).__name__
            logger.warning(
                "Agent error (attempt %d/%d) for user %s: %s",
                attempt + 1,
                1 + MAX_RETRIES,
                user_id,
                error_msg,
            )

            # Catch rate limit / quota errors gracefully
            if (
                "429" in error_msg
                or "RESOURCE_EXHAUSTED" in error_msg
                or "QuotaExceeded" in error_msg
            ):
                return (
                    "⚠️ **Rate Limit Exceeded**\n\n"
                    "You've hit the API quota limit. Please wait a bit before trying again. "
                    "If this persists, check your billing details or rate limits."
                )

            # Catch token limit / context window errors
            if "token count exceeds" in error_msg.lower() or "400" in error_msg:
                logger.error(
                    "Context limit reached for session %s: %s", session_id, error_msg
                )
                return (
                    "⚠️ **Context Limit Reached**\n\n"
                    "The conversation has become too large for me to process. "
                    "Please use the **/reset** command to start a fresh session or wait a few minutes."
                    f"Error: {error_msg}"
                )

            if attempt < MAX_RETRIES:
                parts.clear()
                message_arg = types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=(
                                f"Your previous action failed with this error: {error_msg}\n"
                                "Analyze what went wrong and retry my original request. "
                                "If a tool caused the error, do NOT use it again."
                            )
                        )
                    ],
                )
                continue
            return (
                f"Agent error after {1 + MAX_RETRIES} attempts. Last error: {error_msg}"
            )

    final_response = (
        "\n".join(parts)
        if parts
        else "I processed your request but have no response to show."
    )

    # Check for manual session refresh signal
    refresh_mode = get_pending_refresh(session_id)
    if refresh_mode:
        logger.info(
            "Manual session refresh (%s) triggered for %s", refresh_mode, session_id
        )
        refresh_msg = await _perform_session_refresh(
            runner, user_id, session_id, refresh_mode
        )
        final_response += f"\n\n--- SESSION REFRESHED ---\n{refresh_msg}"

    return final_response


async def process_message_for_context(runner, user_id: str, session_id: str, message: str | types.Content):
    """Silently add a message as context to the session without triggering the agent."""
    import time
    try:
        from google.adk.events.event import Event
    except ImportError:
        # Fallback if Event is not directly importable or moved
        import sys
        Event = sys.modules['google.adk.events.event'].Event
        
    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
    
    if isinstance(message, str):
        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        content = message
        
    event = Event(
        timestamp=time.time(),
        content=content
    )
    await runner.session_service.append_event(session, event)



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
                    
                    user_id = f"tg_{from_user.get('id', 'unknown')}"
                    # In group chats, use the chat_id as the user_id so context is shared
                    session_user_id = f"tg_chat_{chat_id}"
                    session_id = f"tg_chat_{chat_id}"
                    
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
                    
                    if not text and not file_id:
                        continue
                        
                    enriched_text = f"Message from {display_name} ({user_id}): {text} {file_info_text}".strip()
                    
                    message_content = types.Content(role="user", parts=[types.Part.from_text(text=enriched_text)])
                    
                    if file_id:
                        file_data = await download_telegram_file(client, token, file_id)
                        if file_data:
                            blob_bytes, mime_type, filename = file_data
                            message_content.parts.append(
                                types.Part(inline_data=types.Blob(data=blob_bytes, mime_type=mime_type))
                            )

                    # SECURE KEY CAPTURE: intercept before anything reaches the agent
                    from app.secure_config import capture_key, check_pending

                    if check_pending(session_id):
                        result = capture_key(session_id, text)
                        await delete_message(client, token, chat_id, message_id)
                        await send_message(client, token, chat_id, result["message"])
                        continue

                    # SESSION RESET: intercept the user's choice
                    if session_id in _pending_session_reset:
                        runner = get_runner_fn()
                        if runner:
                            result = await _handle_session_reset(
                                runner, session_user_id, session_id, text
                            )
                            await send_message(client, token, chat_id, result)
                        else:
                            _pending_session_reset.pop(session_id, None)
                        continue

                    # Handle /rollback command
                    if text.strip() == "/rollback":
                        trigger_file = os.path.abspath("./data/.rollback_trigger")
                        import json

                        with open(trigger_file, "w") as f:
                            json.dump(
                                {"notify": {"type": "telegram", "chat_id": chat_id}}, f
                            )
                        await send_message(
                            client,
                            token,
                            chat_id,
                            "🔄 **Rollback Triggered**\n\n"
                            "I'm resetting to the previous commit and restarting. "
                            "This will take about a minute. I'll notify you when I'm back online.",
                        )
                        continue

                    # Handle /reset command
                    if text.strip() == "/reset":
                        runner = get_runner_fn()
                        if runner:
                            result = await _perform_session_refresh(
                                runner, session_user_id, session_id, "fresh"
                            )
                            await send_message(
                                client,
                                token,
                                chat_id,
                                f"🧹 **Session Reset**\n{result}",
                            )
                        else:
                            await send_message(
                                client, token, chat_id, "Error: Runner not available."
                            )
                        continue

                    # Handle /init command
                    if text.strip().startswith("/init"):
                        result = process_init_fn(text)
                        await send_message(client, token, chat_id, result)
                        continue
                        
                    if text.lower().strip() == "/think on":
                        await update_session_state(
                            runner=runner,
                            session_id=session_id,
                            user_id=session_user_id,
                            state_delta={"use_planner": True},
                        )
                        await send_message(
                            client, token, chat_id, "🧠 Planner/Thinking mode enabled."
                        )
                        continue

                    if text.lower().strip() == "/think off":
                        await update_session_state(
                            runner=runner,
                            session_id=session_id,
                            user_id=session_user_id,
                            state_delta={"use_planner": False},
                        )
                        await send_message(
                            client, token, chat_id, "⚡ Planner/Thinking mode disabled."
                        )
                        continue

                    # Handle /start command
                    if text.strip() == "/start":
                        await send_message(
                            client,
                            token,
                            chat_id,
                            "Welcome to the Amazon Manager Bot! Send me a message to get started.",
                        )
                        continue

                    runner = get_runner_fn()
                    if not runner:
                        await send_message(
                            client,
                            token,
                            chat_id,
                            "Bot is not fully configured yet. Please visit the /setup page.",
                        )
                        continue

                    is_group = chat_type in ["group", "supergroup", "channel"]
                    is_mentioned = bot_username and (f"@{bot_username}" in text)
                    if is_group and not is_mentioned:
                        logger.info("Silently adding group message for context to session %s", session_id)
                        asyncio.create_task(process_message_for_context(runner, session_user_id, session_id, message_content))
                        continue

                    # Show typing indicator while the agent processes
                    async def keep_typing(_chat_id=chat_id):
                        while True:
                            await send_typing(client, token, _chat_id)
                            await asyncio.sleep(4)

                    typing_task = asyncio.create_task(keep_typing())
                    try:
                        response = await extract_agent_response(
                            runner, session_user_id, session_id, message_content, user_id
                        )
                    finally:
                        typing_task.cancel()
                        try:
                            await typing_task
                        except asyncio.CancelledError:
                            pass

                    await send_message(client, token, chat_id, response)

            except httpx.ReadTimeout:
                continue
            except asyncio.CancelledError:
                logger.info("Telegram poller shutting down")
                return
            except Exception:
                logger.exception("Telegram poller error, retrying in 5s")
                await asyncio.sleep(5)
