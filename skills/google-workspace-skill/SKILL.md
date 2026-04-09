---
name: google-workspace-skill
description: "Google Drive, Sheets, and Calendar integration — per-user OAuth2 connection, file management, spreadsheet operations, calendar management."
---

# Google Workspace Skill

Per-user Google Drive, Sheets, and Calendar access via OAuth2 device code flow. Each user connects their own Google account — they see their own files and calendars.

## Connection Flow

- [ ] Step 1: User says "connect my Google" (or similar)
- [ ] Step 2: Call `google_connect` — returns a URL and code
- [ ] Step 3: Tell the user to open the URL and enter the code
- [ ] Step 4: User confirms they authorized → call `google_connect_complete`
- [ ] Step 5: Done — all Drive/Sheets/Calendar tools now work for that user

One connection grants access to Drive, Sheets, AND Calendar. No need to connect separately.

## Tools

### Connection
| Tool | Purpose |
|------|---------|
| `google_connect` | Start authorization — returns URL + code |
| `google_connect_complete` | Finish authorization after user confirms |
| `google_disconnect` | Remove stored tokens for current user |

### Drive
| Tool | Purpose |
|------|---------|
| `drive_list_files(query, folder_id)` | Search/list files. Query uses Drive API syntax. |
| `drive_download_file(file_id)` | Download a file. Google Docs export as PDF, Sheets as CSV. |

### Sheets
| Tool | Purpose |
|------|---------|
| `sheets_read(spreadsheet_id, range)` | Read data from a range (A1 notation) |
| `sheets_write(spreadsheet_id, range, values)` | Write data to a range |
| `sheets_create(title)` | Create a new spreadsheet |

### Calendar
| Tool | Purpose |
|------|---------|
| `calendar_list` | List all calendars the user has access to |
| `calendar_list_events(days, calendar_id, query)` | List upcoming events (default: next 7 days) |
| `calendar_create_event(title, start_datetime, end_datetime, ...)` | Create a new event with optional attendees, location, description |
| `calendar_update_event(event_id, ...)` | Update an existing event (only provided fields change) |
| `calendar_delete_event(event_id)` | Delete an event |

## Calendar Usage

- **Calendar ID:** Use `'primary'` for the user's main calendar (default). Call `calendar_list` to discover shared/team calendars.
- **Date format:** ISO 8601 — `'2026-04-10T10:00:00'`. Always include time.
- **Timezone:** Pass `timezone_str` (e.g. `'America/New_York'`) when creating/updating events. If omitted, uses the calendar's default timezone. Always call `get_current_time` to know the user's timezone before scheduling.
- **Attendees:** Comma-separated email addresses (e.g. `'alice@mellanni.com,bob@mellanni.com'`). Invitations are sent automatically by Google.
- **All-day events:** For all-day events, use date format `'2026-04-10'` instead of datetime.

## Live References

- [Google Drive API v3](https://developers.google.com/drive/api/reference/rest/v3)
- [Google Sheets API v4](https://developers.google.com/sheets/api/reference/rest)
- [Google Calendar API v3](https://developers.google.com/calendar/api/v3/reference)
- [OAuth 2.0 Device Flow](https://developers.google.com/identity/protocols/oauth2/limited-input-device)

## Gotchas

- **User must connect first.** All tools return "not connected" if the user hasn't authorized. Don't retry — tell them to run the connect flow.
- **Per-user access.** Each user sees only their own files and calendars. Tokens are stored per-email.
- **Existing users must reconnect** after Calendar was added (new scope). If Calendar tools fail with permission errors, tell the user to run `google_connect` again.
- **Spreadsheet ID is in the URL.** `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit` — extract the ID between `/d/` and `/edit`.
- **Range format.** Use A1 notation: `Sheet1!A1:D10`, `Sheet1`, `A1:B5`. Sheet name is optional if there's only one sheet.
- **Tokens auto-refresh.** Access tokens expire after 1 hour but refresh automatically. If refresh fails, the user needs to reconnect.
- **OAuth client must be configured.** Needs `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in vault. Set via `/init`.
- **Calendar API must be enabled** in the Google Cloud project (console.cloud.google.com → APIs & Services → Calendar API).
