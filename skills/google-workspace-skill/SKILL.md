---
name: google-workspace-skill
description: "Google Drive, Sheets, Calendar, and Gmail integration — per-user OAuth2 connection, file management, spreadsheet operations, calendar management, read-only Gmail access."
---

# Google Workspace Skill

Per-user Google Drive, Sheets, Calendar, and Gmail access via OAuth2 device code flow. Each user connects their own Google account — they see their own files, calendars, and mailbox. Gmail is read-only for now.

## Connection Flow

- [ ] Step 1: User says "connect my Google" (or similar)
- [ ] Step 2: Call `google_connect` — returns an authorization URL
- [ ] Step 3: Tell the user to open the URL and sign in
- [ ] Step 4: Google redirects to our callback, tokens are persisted automatically — no follow-up tool call needed
- [ ] Step 5: Done — all Drive/Sheets/Calendar/Gmail tools now work for that user

One connection grants access to Drive, Sheets, Calendar, AND Gmail. No need to connect separately.

## Tools

### Connection
| Tool | Purpose |
|------|---------|
| `google_connect` | Start authorization — returns a browser URL to sign in. Tokens are persisted automatically by the callback route. |
| `google_disconnect` | Remove stored tokens for current user |

### Drive
| Tool | Purpose |
|------|---------|
| `drive_list_files(query, folder_id)` | Search/list files. Query uses Drive API syntax. `folder_id` accepts a bare ID OR a folder URL. |
| `drive_download_file(file_id)` | Download a file. `file_id` accepts a bare ID OR any Drive/Docs/Slides/Sheets URL. Docs/Slides export as text/plain, Sheets as CSV, Drawings as PNG. |

### Sheets
| Tool | Purpose |
|------|---------|
| `sheets_read(spreadsheet_id, range="")` | Read data. `spreadsheet_id` accepts a bare ID OR a Sheets URL. Empty `range` auto-selects the first tab. |
| `sheets_write(spreadsheet_id, range, values)` | Write data. Same URL-or-ID input. |
| `sheets_create(title)` | Create a NEW spreadsheet. DO NOT call this as a fallback when `sheets_read` fails — report the read error instead. |
| `sheets_list_tabs(spreadsheet_id)` | List every tab name. Call BEFORE `sheets_read` when the tab name is unknown. Many real spreadsheets do NOT have a tab called "Sheet1". |

### URL-or-ID rule (ALL Drive tools)

Every ID-taking tool accepts a URL too. **Pass the user's URL directly — never retype the ID.** Google Drive IDs are 25-80 random characters; LLMs typo them reliably (g↔q, 9↔0 confusion). Production failure 2026-05-12: bot emitted `LQq…` after user pasted URL with `LQg…` one message earlier. Tools extract the canonical ID server-side from any of these shapes:

- `https://docs.google.com/spreadsheets/d/<ID>/edit?...`
- `https://docs.google.com/document/d/<ID>/edit?...`
- `https://docs.google.com/presentation/d/<ID>/edit?...`
- `https://docs.google.com/forms/d/<ID>/...`
- `https://docs.google.com/drawings/d/<ID>/...`
- `https://drive.google.com/file/d/<ID>/view?...`
- `https://drive.google.com/drive/folders/<ID>?...`
- `https://drive.google.com/open?id=<ID>`
- Bare 25-80 char ID

### Calendar
| Tool | Purpose |
|------|---------|
| `calendar_list` | List all calendars the user has access to |
| `calendar_list_events(days, calendar_id, query)` | List upcoming events (default: next 7 days) |
| `calendar_create_event(title, start_datetime, end_datetime, ...)` | Create a new event with optional attendees, location, description |
| `calendar_update_event(event_id, ...)` | Update an existing event (only provided fields change) |
| `calendar_delete_event(event_id)` | Delete an event |

### Gmail (read-only)
| Tool | Purpose |
|------|---------|
| `gmail_list_labels()` | Enumerate labels. Returns system (INBOX, SENT, etc.) and user-defined labels. |
| `gmail_list_messages(query, max_results, label_ids)` | Search messages. Uses Gmail search syntax. |
| `gmail_get_message(message_id, full)` | Full headers + body + attachment metadata. Body truncated to 8000 chars unless `full=True`. |
| `gmail_list_threads(query, max_results)` | Thread-centric list — useful for back-and-forth conversations. |
| `gmail_get_thread(thread_id, full)` | All messages in a thread. Each body truncated per `full`. |
| `gmail_download_attachment(message_id, attachment_id, filename)` | Save an attachment to `./tmp/gmail_attachments/`. Cached 24h by default. |

## Calendar Usage

- **Calendar ID:** Use `'primary'` for the user's main calendar (default). Call `calendar_list` to discover shared/team calendars.
- **Date format:** ISO 8601 — `'2026-04-10T10:00:00'`. Always include time.
- **Timezone:** Pass `timezone_str` (e.g. `'America/New_York'`) when creating/updating events. If omitted, uses the calendar's default timezone. Always call `get_current_time` to know the user's timezone before scheduling.
- **Attendees:** Comma-separated email addresses (e.g. `'alice@mellanni.com,bob@mellanni.com'`). Invitations are sent automatically by Google.
- **All-day events:** For all-day events, use date format `'2026-04-10'` instead of datetime.

## Gmail Usage

- **Search syntax:** Use Gmail's native search operators — `from:`, `to:`, `subject:`, `is:unread`, `has:attachment`, `newer_than:7d`, `label:INBOX`. Combine freely: `from:amazon.com is:unread newer_than:3d`.
- **Body truncation:** By default, bodies are clipped to 8000 chars and end with a `[truncated, N more chars — call with full=True]` marker. Call the tool again with `full=True` when you genuinely need the whole body.
- **Attachments:** `gmail_download_attachment` writes to `./tmp/gmail_attachments/{message_id}_{filename}`. Files older than 24h are purged automatically on the next download (override with `GMAIL_ATTACHMENT_TTL_HOURS`). The cache is not a Drive upload — it's a local scratch area.
- **Threads vs messages:** Prefer `gmail_list_threads` + `gmail_get_thread` for buyer↔seller or supplier conversations; use messages tools for one-shot notifications (Amazon alerts, receipts).
- **Read-only:** Sending, drafting, labeling, and archiving aren't available yet. If the user asks to send a reply, tell them this is a read-only integration for now.

## NOT Your Job: Personal Reminders

Google Calendar is for **scheduled events, meetings, and appointments with other people or time-blocked activities visible on the user's calendar**. It is NOT a personal reminder system for the agent.

If the user says "remind me…", "nudge me…", "ping me at…", "every day do X", "in 10 minutes…", or any similar reminder phrasing **without explicitly asking for a calendar event** (no attendees, no meeting, no calendar mentioned), this is a scheduling request, not a calendar event. Do NOT call `calendar_create_event`. Instead, transfer back to the parent (`AmazonHeadAgent` → `CoordinatorAgent`) so the coordinator can use its scheduling tools.

Create a calendar event only when the user explicitly says "add to my calendar", "create a calendar event", mentions attendees/invitations, or is clearly describing a meeting.

## Live References

- [Google Drive API v3](https://developers.google.com/drive/api/reference/rest/v3)
- [Google Sheets API v4](https://developers.google.com/sheets/api/reference/rest)
- [Google Calendar API v3](https://developers.google.com/calendar/api/v3/reference)
- [OAuth 2.0 Web Server Flow](https://developers.google.com/identity/protocols/oauth2/web-server) (Authorization Code + PKCE)

## Gotchas

- **User must connect first.** All tools return "not connected" if the user hasn't authorized. Don't retry — tell them to run the connect flow.
- **Per-user access.** Each user sees only their own files and calendars. Tokens are stored per-email.
- **Existing users must reconnect** after the OAuth flow migrated from device-flow to web-flow (so Gmail scopes are supported). If any Google tool fails with permission or invalid-grant errors, tell the user to run `google_connect` again.
- **Spreadsheet ID is in the URL.** `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit` — but you do NOT need to extract it. Pass the FULL URL to `sheets_read` / `sheets_write` / `sheets_list_tabs`; the tool extracts the ID server-side. Manual extraction is a typo trap.
- **Range format.** Use A1 notation: `Sheet1!A1:D10`, `Sheet1`, `A1:B5`. Sheet name is optional if there's only one sheet.
- **Tokens auto-refresh.** Access tokens expire after 1 hour but refresh automatically. If refresh fails, the user needs to reconnect.
- **OAuth client must be configured.** Needs `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and `OAUTH_BASE_URL` (the public URL of the bot's A2A Starlette server, e.g. `https://bezosapp.uk`) in vault. Set via `/init`. The OAuth client in Google Cloud Console must be type "Web application" with `{OAUTH_BASE_URL}/oauth/google/callback` as an authorized redirect URI.
- **Calendar API must be enabled** in the Google Cloud project (console.cloud.google.com → APIs & Services → Calendar API).
- **Gmail API must be enabled** in the Google Cloud project (console.cloud.google.com → APIs & Services → Gmail API).
