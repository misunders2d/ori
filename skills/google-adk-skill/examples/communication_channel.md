# Secure Communication Channel Patterns

When building new integration interfaces (such as Slack, Discord, Google Chat) that support group channels, the architecture dictates a specific approach to maintain securely decoupled Session context (conversation history) and Caller Identity (the actual user typing the message).

## The State Injection Pattern

To ensure Admin Guardrails block unauthorized individuals in group settings, **DO NOT** simply pass the group chat ID as the `user_id` when evaluating prompts. 

You must natively isolate true caller IDs from group/channel IDs. 

**Example Implementation (from `telegram_poller.py`):**
1. The `session_id` and the primary `user_id` in the `runner.run_async()` are set to the *Group Chat ID* so that all users in the chat share the same conversation history.
2. However, *before* evaluating the message, the exact individual sender's true ID is forcefully injected into the memory state via an internal ADK `EventActions(state_delta={...})` hook.

```python
# 1. Identify true caller vs group context
actual_caller_id = "tg_user_12345"
session_group_id = "tg_chat_-98765432"

# 2. Force inject the true caller into session memory BEFORE running the LLM
await update_session_state(
    runner=runner,
    session_id=session_group_id,
    user_id=session_group_id,
    state_delta={"user_id": actual_caller_id},
)

# 3. Evaluate prompt normally
async for event in runner.run_async(
    user_id=session_group_id,
    session_id=session_group_id,
    new_message=message_arg,
):
    ...
```

By replicating this explicit injection pattern, the `admin_only_guardrail` correctly intercepts and blocks any unauthorized individual talking in the group chat, because the `callback_context.state["user_id"]` perfectly mirrors `actual_caller_id`.
