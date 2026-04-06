---
name: google-workspace-skill
description: "Google Drive and Sheets integration — per-user OAuth2 connection, file management, spreadsheet operations."
---

# Google Workspace Skill

Per-user Google Drive and Sheets access via OAuth2 device code flow. Each user connects their own Google account.

## Connection Flow

- [ ] Step 1: User says "connect my Google Drive" (or similar)
- [ ] Step 2: Call `google_connect` — returns a URL and code
- [ ] Step 3: Tell the user to open the URL and enter the code
- [ ] Step 4: User confirms they authorized → call `google_connect_complete`
- [ ] Step 5: Done — all Drive/Sheets tools now work for that user

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

## Gotchas

- **User must connect first.** All tools return "not connected" if the user hasn't authorized. Don't retry — tell them to run the connect flow.
- **Per-user access.** Each user sees only their own files. Tokens are stored per-email.
- **Spreadsheet ID is in the URL.** `https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit` — extract the ID between `/d/` and `/edit`.
- **Range format.** Use A1 notation: `Sheet1!A1:D10`, `Sheet1`, `A1:B5`. Sheet name is optional if there's only one sheet.
- **Tokens auto-refresh.** Access tokens expire after 1 hour but refresh automatically. If refresh fails, the user needs to reconnect.
- **OAuth client must be configured.** Needs `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in vault. Set via `/init`.
