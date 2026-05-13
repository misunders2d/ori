---
name: approval-skill
description: "How to handle privileged tool calls that require explicit admin approval (ACT-XXXXXX tokens, optional TOTP 2FA). Load this when a tool returns an action token or when the user replies 'Approve ACT-...'."
---

# Approval Protocol

Privileged actions (delete, force-push, mass-modify, send-public-message, etc.) don't execute immediately. The plugin layer (`AdminGatePlugin`) intercepts the tool call and returns an action token instead.

## How it looks

When a privileged tool is invoked, the response is something like:

```
Action requires approval. Token: ACT-A7B3F9
Summary: <human-readable description of what will happen>
To approve: send "Approve ACT-A7B3F9" (and your 6-digit code if 2FA is enabled).
```

The actual tool **has not run** yet. The action is queued in `data/pending_actions.db`.

## What you do

1. Relay the token + summary to the user verbatim. Don't summarize the summary further; the user needs to know exactly what they're approving.
2. Wait for the user to reply with `Approve ACT-XXXXXX` (case-insensitive, in any language) or to cancel.
3. When they approve, call:

   ```python
   execute_approved_action(token="ACT-A7B3F9")
   # Or with TOTP if enabled:
   execute_approved_action(token="ACT-A7B3F9", totp_code="123456")
   ```

4. Relay the result back.

## TOTP (optional 2FA)

If `ADMIN_TOTP_SECRET` is set in the vault and `REQUIRE_2FA=true`, the user must include a 6-digit TOTP code with their approval. Pass both `token` and `totp_code` to `execute_approved_action`.

If they forget the code, the approval is rejected — ask for both again.

## Anti-patterns

- Don't auto-approve. Ever. The user typing "approve" or similar is required.
- Don't try to bypass by re-invoking the original tool — `AdminGatePlugin` will just issue another token.
- Don't suppress the summary. The summary is the user's only window into what they're approving.
- Tokens expire (default 15 min) — if the user takes too long, ask them to re-issue the request.

## Pre-LLM intercept (2026-05-13)

When the user types `Approve ACT-XXXXXX [123456]`, the Slack and
Telegram pollers detect the pattern via
`app/core/approval_intercept.py:parse_approval_text` and call
`handle_approval(...)` directly, BEFORE the agent runs. The agent
never sees the approval message. The intercept:

1. Validates the token belongs to the requesting user.
2. Enforces TOTP if `ADMIN_TOTP_SECRET` is set and `REQUIRE_2FA=true`.
3. Pops the staged action from `data/pending_actions.db` (single-use).
4. Calls the staged tool with a synthetic `ApprovalContext` shim.
5. Posts the tool's reply back to the chat (Slack: in the same thread).

This eliminates the 2026-05-13 loop where the LLM, on seeing
`Approve ACT-XXXXXX`, would re-invoke the original gated tool and
the admin guardrail would stage a fresh token each turn —
effectively un-consumable.

The skill instructions above still apply when the LLM is the one
calling `execute_approved_action` (for example, on a platform
without a deterministic intercept, or when staging is announced
mid-conversation). The intercept is a deterministic short-circuit,
not a replacement.

`stage_action` is also idempotent: a re-invocation with identical
`(tool, user, session, args)` returns the SAME pending token (until
expiry), so even if the LLM re-fires the original tool, the user's
earlier approval still matches the latest staged record.

## Cancellation

If the user says "cancel" or "no" before approving:
- Tell them the action was cancelled (no code change needed; pending actions auto-expire from `pending_actions.db`).
- Ask if they want to do something else.
