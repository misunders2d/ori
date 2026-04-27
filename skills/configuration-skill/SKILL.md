---
name: configuration-skill
description: "How to add, list, or remove API keys and OAuth integrations through the chat. Load this when the user asks to add/set/configure/connect a key, token, credential, or third-party service (Slack, Telegram, OpenRouter, Anthropic, Google, GitHub, ClickUp, …)."
---

# Configuration Protocol

The Coordinator owns this end-to-end. Do not delegate. `configure_integration`, `list_integrations`, and `remove_integration` are your tools.

## Cardinal rules

1. **ONE approval at a time.** Never generate two ACT tokens in the same turn — the user will conflate them. If the integration needs N keys, do them sequentially: approve → send key → confirm saved → next approval.
2. **Don't pad.** If the user says "add my Slack tokens", set ONLY what they asked for. Don't proactively ask "want to also configure SLACK_SIGNING_SECRET?" unless they need it for the path they're using.
3. **Recognize what they're handing you.** "I have app token and bot token" → Slack Socket Mode → set `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`, done. Don't offer them an OAuth flow.
4. **No stale confirmations.** Don't reissue "Configured X successfully" if the user has already moved on to the next message.

## How configure_integration works

Two routing paths, picked by name:

| Path | Trigger | Mechanism |
|---|---|---|
| **Flat token** | `name` is an UPPERCASE env-key (e.g. `SLACK_BOT_TOKEN`) | Secure-capture: user's NEXT message is intercepted by transport, written to vault, never reaches the LLM. |
| **OAuth provider** | `name` is a lowercase provider id (`google`, `github`, `slack`, `clickup`) | Tool returns `authorize_url`; user clicks, completes OAuth in browser; callback handler finalizes the token into the vault. |

Both go through the same tool. Sequence matters: always `configure_integration("KEY_NAME")` BEFORE telling the user to send the value — otherwise secure-capture isn't armed.

## Per-integration recipes

### Slack — three paths, pick ONE

The user almost always wants Socket Mode (no public URL needed).

**Path A — Socket Mode (recommended, simplest):**
- Needs: `SLACK_BOT_TOKEN` (starts with `xoxb-`) and `SLACK_APP_TOKEN` (starts with `xapp-`).
- The `SLACK_APP_TOKEN` is the SOCKET-MODE app-level token from the Slack app's "Basic Information → App-Level Tokens" page, not the OAuth client.
- `SLACK_SIGNING_SECRET` is **NOT NEEDED** for Socket Mode. Don't ask for it.
- Order: configure `SLACK_BOT_TOKEN` (one approval, one token send) → confirm saved → configure `SLACK_APP_TOKEN` (second approval, second token send) → done.

**Path B — HTTP Events (requires public URL, advanced):**
- Needs: `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET`.
- Only relevant if the user has explicitly chosen webhook delivery. Default is Socket Mode.

**Path C — Multi-tenant OAuth:**
- Needs: `SLACK_OAUTH_CLIENT_ID` and `SLACK_OAUTH_CLIENT_SECRET`. Then `configure_integration("slack")` to start the OAuth flow.
- Rare. Only for installs where each user authenticates separately.

**Recognizing user intent:**
- "I have app token and bot token" → Path A. Don't offer the others.
- "I want to use OAuth" / "multi-tenant" → Path C.
- "I'll deploy a webhook URL" → Path B.

### Telegram

- Needs: `TELEGRAM_BOT_TOKEN` (from @BotFather). One token, one approval.
- Optional: `TELEGRAM_WEBHOOK_SECRET` (only if running webhook mode instead of long-poll).

### GitHub

**Personal Access Token (simpler):**
- `GITHUB_TOKEN` — a fine-grained PAT or classic PAT with `repo` scope.
- Optional: `GITHUB_REPO` (the `owner/repo` to push evolutions to).

**OAuth (multi-tenant):**
- `GITHUB_OAUTH_CLIENT_ID` + `GITHUB_OAUTH_CLIENT_SECRET`, then `configure_integration("github")`.

### Google (LLM provider)

Three modes, mutually exclusive:

- **Direct API key:** `GOOGLE_API_KEY` from aistudio.google.com. One token, one approval. Set `GOOGLE_GENAI_USE_VERTEXAI` to `FALSE` (or leave unset).
- **Vertex AI:** `GOOGLE_GENAI_USE_VERTEXAI=TRUE` + `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION` + ADC (configured via `gcloud auth application-default login` outside the bot).
- **Per-user OAuth (Drive/Gmail/Calendar/Sheets):** ADMIN sets `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` + `OAUTH_BASE_URL` once via `/init`. After that, INDIVIDUAL USERS connect their own accounts by saying "connect my Google" — delegate to `AmazonHeadAgent` → `AmazonWorkspaceAgent` which calls `google_connect`. Do NOT use `configure_integration("google")` — that's the multi-tenant admin path, not how regular users link their accounts.

### Anthropic / OpenAI / OpenRouter

- One key each: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`. One approval, one send.

### ClickUp (when present)

- Personal token: `CLICKUP_API_TOKEN` + optional `CLICKUP_TEAM_ID`. Single-user.
- OAuth: `CLICKUP_OAUTH_CLIENT_ID` + `CLICKUP_OAUTH_CLIENT_SECRET`, then `configure_integration("clickup")`.

### Domain integrations (when present in this build)

| Service | Keys |
|---|---|
| Keepa | `KEEPA_API_KEY` |
| Helium 10 | `H10_API_KEY` |
| YouTube | `YOUTUBE_API_KEY` |
| Amazon SP-API | `LWA_REFRESH_TOKEN`, `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `SP_API_REGION`, `SP_API_MARKETPLACE_ID` |
| BigQuery (service account JSON) | `BQ_GCP_SERVICE_ACCOUNT_INFO` |
| Neo4j | `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` |

All flat-token: one approval, one send, per key.

## Sequential approval pattern

For multi-key integrations (e.g. Slack Socket Mode = 2 keys):

```
You: configure_integration("SLACK_BOT_TOKEN")
     → tool emits ACT-XXXXXX
     → you tell user: "Approve ACT-XXXXXX, then send SLACK_BOT_TOKEN."
User: Approve ACT-XXXXXX
User: <sends token>
Bot:  Saved. (transport handles this, you don't print "Configured X" again)

You: "SLACK_BOT_TOKEN saved. Now adding SLACK_APP_TOKEN."
You: configure_integration("SLACK_APP_TOKEN")
     → tool emits ACT-YYYYYY
     → you tell user: "Approve ACT-YYYYYY, then send SLACK_APP_TOKEN."
User: Approve ACT-YYYYYY
User: <sends token>
Bot:  Saved.

You: "Slack connected. Both tokens are in. Restart not required."
```

**Never** generate ACT-XXXXXX and ACT-YYYYYY in the same turn. The user will paste both `Approve` lines together and the second one will hit a transport that's still in "send the value" mode for the first key. Disaster.

## Listing & removing

- `list_integrations()` — show what's currently configured.
- `remove_integration(name)` — delete one. Confirm with the user before destructive removal.

## What NOT to do

- Don't read code or other skills to "figure out how" — `configure_integration` is the entry point.
- Don't ask the user to paste keys directly without arming `configure_integration` first.
- Don't delegate to DeveloperAgent.
- Don't use `/init` from your side — that's a transport-level escape hatch for offline-LLM recovery.
- Don't proactively ask for keys the user didn't mention.
- Don't generate two ACT tokens in the same turn.

## Quick reference

```
User: "add my OpenRouter key"
You: configure_integration("OPENROUTER_API_KEY") → "Approve ACT-… then send the key."
User: Approve ACT-…
User: <pastes key>
You: "Saved."
```

```
User: "I have app token and bot token, connect Slack"
You: "Socket Mode it is. Two approvals + two messages, sequential."
You: configure_integration("SLACK_BOT_TOKEN") → "Approve ACT-… then send the bot token."
…
You: configure_integration("SLACK_APP_TOKEN") → "Approve ACT-… then send the app token."
…
You: "Slack connected."
```

```
User: "connect Google Drive"
You: configure_integration("google") → returns authorize URL
You to user: "Click here: <url>"
User: <completes OAuth in browser>
You: "Connected."
```
