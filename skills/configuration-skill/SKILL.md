---
name: configuration-skill
description: "How to add, list, or remove API keys and OAuth integrations through the chat. Load this when the user asks to add/set/configure/connect a key, token, credential, or third-party service (OpenRouter, Anthropic, Google, GitHub, Telegram, …)."
---

# Configuration Protocol

The Coordinator owns this end-to-end. Do not delegate to DeveloperAgent and do not search for other skills — `configure_integration`, `list_integrations`, and `remove_integration` are tools you already have.

## Two kinds of credentials

| Kind | Examples | Mechanism |
|---|---|---|
| **Flat token** | `OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, `OPENAI_API_KEY` | Secure-capture: the user's NEXT message is intercepted by the transport layer, written to the vault, and NEVER reaches the LLM. |
| **OAuth provider** | `google`, `github` | Authorize URL flow: the user clicks a link, grants access; the OAuth callback finalizes the token into the vault. |

Both flow through the same tool — `configure_integration(name)` — which routes by `name`.

## Flat token procedure

1. Confirm with the user which key they want to add (e.g. "OPENROUTER_API_KEY"). If ambiguous, ask before acting.
2. Call `configure_integration(name="OPENROUTER_API_KEY")`. It returns `{"status": "awaiting_input", "key_name": "OPENROUTER_API_KEY", ...}`.
3. Tell the user: send the raw key as their **next message**. Do not include any prefix, prose, or formatting.
4. Done. The transport layer captures it, writes to vault, and (where supported, e.g. Telegram) deletes the message from chat history. Confirm to the user with: "Saved. Restart not required for new conversations."

**Common flat-token names (case-insensitive — wrapped to UPPERCASE internally):**
`OPENROUTER_API_KEY`, `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, `GITHUB_REPO`, `BOT_NAME`.

If the key name isn't recognized the tool returns `UNKNOWN_INTEGRATION` with the full allowed list — relay that to the user verbatim.

## OAuth provider procedure

1. Call `configure_integration(name="google")` (or `"github"`).
2. The tool returns `status: "awaiting_user"` plus an `authorize_url`. Send the URL to the user.
3. The user opens it, grants access. The OAuth callback handler at `/oauth/<provider>/callback` finalizes the token into the vault automatically — no further action needed from you.
4. Confirm with the user once they say they're done. Optionally call `list_integrations` to verify.

If the tool returns `PROVIDER_NOT_CONFIGURED`, the OAuth client credentials themselves are missing. Tell the user to set `OAUTH_<PROVIDER>_CLIENT_ID` and `OAUTH_<PROVIDER>_CLIENT_SECRET` first (these go through the same flat-token flow).

## Listing & removing

- `list_integrations()` — show what's configured (vault keys + OAuth tokens).
- `remove_integration(name)` — delete one. Confirm with the user before destructive removal (per the DESTRUCTIVE = EXPLICIT APPROVAL rule).

## What NOT to do

- Don't read code or skills to "figure out how" — `configure_integration` is the single entry point.
- Don't ask the user to paste keys directly into chat — that exposes them to the LLM. Always go through `configure_integration` first to arm secure-capture.
- Don't delegate to DeveloperAgent. The Coordinator owns this.
- Don't use `/init` from your side — that's a transport-level escape hatch for when the LLM is offline. The user can use it on their side; you should not generate it.

## Quick reference

```
User: "add my OpenRouter key"
You: configure_integration("OPENROUTER_API_KEY") → "Send your OPENROUTER_API_KEY as your next message."
User: <pastes key>     ← intercepted by transport, never reaches you
You: "Saved."
```

```
User: "connect Google Drive"
You: configure_integration("google") → returns authorize URL
You to user: "Click here: <url>"
User: <completes OAuth in browser>
You: "Connected." (after they confirm or list_integrations shows it)
```
