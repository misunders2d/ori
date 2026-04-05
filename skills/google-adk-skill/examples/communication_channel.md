# Secure Communication Channel Patterns

When building new integration interfaces (such as Slack, Discord, Google Chat) that support group channels, the architecture dictates a specific approach to maintain securely decoupled Session context (conversation history) and Caller Identity (the actual user typing the message).

> **For the full guide on implementing a new platform**, see `transport_adapter.md`.
> This document focuses specifically on the **security-critical identity isolation pattern**.

## The State Injection Pattern

To ensure Admin Guardrails block unauthorized individuals in group settings, **DO NOT** simply pass the group chat ID as the `user_id` when evaluating prompts.

You must natively isolate true caller IDs from group/channel IDs.

**Example Implementation (using the TransportAdapter):**
1. Use `adapter.make_session_id(channel_id)` for both `user_id` and `session_id` in the runner, so all users in the chat share the same conversation history.
2. Pass the true individual sender's ID as `actual_caller_id` to `extract_agent_response()`, which injects it into session state *before* the LLM evaluates the request.

```python
from app.core.agent_executor import extract_agent_response
from app.core.transport import TransportAdapter

# adapter = your registered TransportAdapter instance

# 1. Build IDs via the adapter
session_id = adapter.make_session_id(channel_id)    # e.g. "tg_chat_-98765432"
actual_caller_id = adapter.make_user_id(sender_id)  # e.g. "tg_12345"

# 2. extract_agent_response handles the state injection automatically
#    when actual_caller_id is provided — it calls update_session_state()
#    to set state_delta={"user_id": actual_caller_id} BEFORE running the LLM
response = await extract_agent_response(
    runner,
    user_id=session_id,       # group context (shared history)
    session_id=session_id,    # group context (shared history)
    message=message_content,
    actual_caller_id=actual_caller_id,  # true sender (for guardrails)
)
```

By using this pattern, the `admin_only_guardrail` and `admin_tool_guardrail` correctly intercept and block any unauthorized individual talking in the group chat, because `callback_context.state["user_id"]` mirrors the true `actual_caller_id`.

## Why This Matters

Without this injection, **all group chat messages appear to come from the same user** (the group ID). An attacker in a group could invoke admin-only tools like `configure_integration` or delegate to the `DeveloperAgent` because the guardrails would see the group ID instead of their personal ID.
