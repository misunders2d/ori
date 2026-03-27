# Transport Adapter Pattern

When adding a new messaging platform (Discord, Slack, WhatsApp, web UI, etc.), you MUST implement the `TransportAdapter` abstract base class and register it via the adapter registry. This ensures all platform-agnostic systems (scheduled task delivery, notification routing, key capture) work automatically without any changes.

## Architecture Overview

```
app/core/transport.py          — TransportAdapter ABC + registry
app/core/agent_executor.py     — Platform-agnostic agent execution
interfaces/telegram_poller.py  — Reference implementation (TelegramAdapter)
```

The system is split into two layers:
1. **Transport layer** (platform-specific): Handles polling/webhooks, message formatting, file downloads, typing indicators
2. **Execution layer** (platform-agnostic): Runs the ADK agent, manages sessions, handles retries

## Step-by-Step: Adding a New Platform

### 1. Implement the TransportAdapter

Create `interfaces/<platform>_poller.py` (or `_webhook.py`):

```python
from app.core.transport import TransportAdapter, register_adapter

class DiscordAdapter(TransportAdapter):

    def __init__(self, client, token: str):
        self._client = client
        self._token = token

    @property
    def platform_name(self) -> str:
        return "discord"

    def make_session_id(self, channel_id: str | int) -> str:
        return f"discord_channel_{channel_id}"

    def make_user_id(self, user_id: str | int) -> str:
        return f"discord_{user_id}"

    def parse_notify_info(self, session_id: str) -> dict:
        if session_id.startswith("discord_channel_"):
            return {
                "type": "discord",
                "channel": session_id.replace("discord_channel_", ""),
            }
        return {}

    async def send_message(self, target_id, text):
        # Platform-specific send logic
        ...

    async def send_typing(self, target_id):
        ...

    async def delete_message(self, target_id, message_id):
        ...

    async def download_file(self, file_id):
        ...
```

### 2. Register the adapter at startup

Inside your polling/webhook loop, before processing any messages:

```python
adapter = DiscordAdapter(client, token)
register_adapter(adapter)
```

This makes the adapter available globally. Scheduled tasks, tools, and other systems will automatically route messages through it based on the session ID prefix.

### 3. Use the agent executor (NOT raw runner calls)

Import and use the platform-agnostic functions from `app/core/agent_executor`:

```python
from app.core.agent_executor import (
    extract_agent_response,
    process_message_for_context,
    update_session_state,
)

# Build session/user IDs via the adapter
session_id = adapter.make_session_id(channel_id)
user_id = adapter.make_user_id(sender_id)

# Run the agent
response = await extract_agent_response(
    runner, session_id, session_id, message_content, user_id
)

# Send the response via the adapter
await adapter.send_message(channel_id, response)
```

### 4. Handle secure key capture

The key capture system is already platform-agnostic. You just need to:

```python
from app.secure_config import capture_key, check_pending

if check_pending(session_id):
    result = capture_key(session_id, text)
    await adapter.delete_message(channel_id, message_id)  # Remove the key from chat
    await adapter.send_message(channel_id, result["message"])
    continue  # Do NOT pass to agent
```

### 5. Handle group chat identity isolation

See `communication_channel.md` for the security pattern. The key rule: use `adapter.make_session_id(channel_id)` for both `user_id` and `session_id` in `runner.run_async()`, but pass the true caller ID via `actual_caller_id` to `extract_agent_response()`. This ensures group context is shared but admin guardrails check the real sender.

### 6. Wire into run_bot.py

Add your poller/webhook as a new task in `run_bot.py` `main()`:

```python
# Discord
if os.environ.get("DISCORD_BOT_TOKEN"):
    from interfaces.discord_poller import poll_discord
    tasks.append(asyncio.create_task(poll_discord(get_runner, process_init_command)))
```

## What You Get for Free

Once registered, these systems work automatically with no additional code:
- **Scheduled task delivery** (`app/tasks.py`) — routes reminders back through your adapter
- **Update/rollback notifications** (`app/tools/system.py`) — notifies the user after deploy/rollback
- **Session management** (`app/core/agent_executor.py`) — session refresh, summarization
- **Key capture** (`app/secure_config.py`) — intercepts secrets before they reach the agent

## Critical Rules

1. **Session ID prefix MUST be unique** per platform (e.g., `tg_chat_`, `discord_channel_`, `slack_channel_`)
2. **Never call `runner.run_async()` directly** — always use `extract_agent_response()` which handles retries, rate limits, and session refresh signals
3. **Always isolate caller identity** from channel identity in group chats (see `communication_channel.md`)
4. **Register the adapter before processing messages** — tools may fire scheduled tasks that need to route back immediately
5. **Delete key messages after capture** — call `adapter.delete_message()` to prevent secrets from staying in chat history
