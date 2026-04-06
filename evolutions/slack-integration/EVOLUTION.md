---
name: slack-integration
description: Full Slack Socket Mode chat interface with transport adapter, poller, and agent tools.
author: Bezos
created: 2026-04-06
verified: true
tags: [slack, chat, messaging, socket-mode, slack-bolt]
files:
  - app/core/transport_slack.py
  - interfaces/slack_poller.py
  - app/tools/slack.py
---

# slack-integration

Full Slack Socket Mode chat interface with transport adapter, real-time poller, and agent-facing tools. Adds Slack as a first-class messaging platform alongside Telegram.

## Usage

### Prerequisites

1. Create a Slack App at https://api.slack.com/apps with Socket Mode enabled.
2. Required scopes: `chat:write`, `channels:read`, `channels:history`, `files:read`, `files:write`, `users:read`.
3. Generate a Bot Token (`xoxb-...`) and App-Level Token (`xapp-...`).
4. Store tokens via `/init` or directly in `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```

### Architecture

- **`app/core/transport_slack.py`** — `SlackAdapter` implementing the `TransportAdapter` interface. Handles message sending (with thread support), file uploads/downloads, secret scrubbing, and SSRF protection.
- **`interfaces/slack_poller.py`** — Socket Mode listener using `slack-bolt`. Handles real-time message events, access control, mid-flight cancellation, session management, file handling, and `/reset` command.
- **`app/tools/slack.py`** — Agent-facing tools: `slack_post_message`, `slack_list_channels`, `slack_read_history`, `slack_read_replies`, `slack_get_user_info`.

### Integration

1. The poller auto-starts if `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set.
2. Register the adapter in your bot's startup (done automatically by the poller).
3. Add Slack tools to CoordinatorAgent's tool list for agent-initiated Slack operations.
4. Add `slack-bolt` to dependencies: `uv add slack-bolt`.

### Security

- All outbound messages pass through secret scrubbing (env values + token patterns).
- File downloads validated against `files.slack.com` (SSRF protection).
- File size enforced at 20MB.
- Access control via whitelist/blacklist system.
- Sensitive messages (keys, TOTP) auto-deleted from Slack history.

## Files

- `app/core/transport_slack.py`
- `interfaces/slack_poller.py`
- `app/tools/slack.py`
