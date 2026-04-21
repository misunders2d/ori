# Gmail Read-Only Support — Design

**Date:** 2026-04-21
**Status:** Approved, pending implementation plan
**Scope:** Extend the existing Google Workspace integration (Drive, Sheets, Calendar) with Gmail read-only tools. Structured so future send/modify tools can drop in without refactoring the read path.

## Goals

- Per-user Gmail read access via the existing OAuth device flow.
- Six read tools covering messages, threads, labels, and attachments.
- Local-disk attachment caching with a time-based sweep, scoped to Gmail (no changes to Drive's existing download behavior).
- Body truncation with caller override to protect agent context from long newsletters and supplier threads.

## Non-Goals

- Sending, drafting, labeling, archiving, trashing — deferred to a later pass.
- Global cleanup across all `./tmp/*` subdirectories — out of scope; retrofitting Drive is a separate decision.
- Push notifications / Gmail watch API — not needed for agent-driven pull workflows.

## Architecture

### Module

New file: `app/tools/google_gmail.py`.

Reuses helpers from `app/tools/google_drive.py`:

- `_get_user_email(tool_context)` — resolves the current user.
- `_get_valid_token(email)` — returns a fresh access token, refreshing if expired.
- `_auth_headers(token)` — Authorization header builder.

Same pattern as `app/tools/google_calendar.py`. Zero new auth infrastructure.

### Internal organization inside `google_gmail.py`

- `_GMAIL_API` constant for base URL.
- `_decode_body(payload)` — walks `payload.parts`, prefers `text/plain`, falls back to HTML stripped via stdlib `html.parser`. No new dependencies.
- `_sweep_attachments()` — mtime-based purge of `./tmp/gmail_attachments/`, called at the start of every `gmail_download_attachment` invocation.
- `_truncate(body: str, full: bool) -> str` — returns body unchanged when `full=True`; otherwise clips to 8000 chars and appends `"...[truncated, N more chars — call with full=True]"`.
- Read tools (below).

Grouping by concern in one file keeps future write tools (send, modify, drafts) addable alongside without touching read code.

### OAuth scope

Add `https://www.googleapis.com/auth/gmail.readonly` to `SCOPES` in `app/tools/google_oauth/device_flow.py:19`.

Existing users must reconnect — the skill doc already captures this pattern for Calendar; we extend it to cover Gmail.

### Toolset registration

Add `FunctionTool` wrappers for all six Gmail tools to `app/toolsets/google_workspace.py`.

## Tool Surface

| Tool | Signature | Returns |
|------|-----------|---------|
| `gmail_list_messages` | `(query: str = "", max_results: int = 25, label_ids: str = "")` | List of `{id, thread_id, snippet, from, subject, date, has_attachments}` — see API Cost below |
| `gmail_get_message` | `(message_id: str, full: bool = False)` | `{id, thread_id, headers, body, attachments: [{id, filename, mime, size}]}` — body truncated to 8000 chars unless `full=True` |
| `gmail_list_threads` | `(query: str = "", max_results: int = 25)` | List of `{id, snippet, message_count, last_date, participants}` — see API Cost below |
| `gmail_get_thread` | `(thread_id: str, full: bool = False)` | `{id, messages: [... same shape as gmail_get_message output ...]}` — each body truncated per `full` |
| `gmail_list_labels` | `()` | List of `{id, name, type: "system" | "user"}` |
| `gmail_download_attachment` | `(message_id: str, attachment_id: str, filename: str = "")` | `{status, file_path, filename, size_bytes}` |

### Query semantics

`query` uses standard Gmail search syntax — `from:`, `to:`, `subject:`, `is:unread`, `has:attachment`, `newer_than:7d`, etc. Passed through unchanged as the `q` parameter.

`label_ids` is a comma-separated string (consistent with how Calendar tools accept `attendees`). Split into a list when building the request.

### API Cost for list tools

Gmail's `messages.list` and `threads.list` endpoints return only `{id, threadId, historyId}` — no snippet, no headers, no date. The rich fields listed in the tool surface above require a follow-up `get` call per result.

`gmail_list_messages` implementation:

1. One `messages.list` call to get up to `max_results` IDs.
2. Up to `max_results` parallel `messages.get` calls with `format=metadata` to fetch headers and snippets. `asyncio.gather` across a shared `httpx.AsyncClient`.
3. Compose the flat list from the parallel results.

`gmail_list_threads` behaves the same way with `threads.list` + `threads.get` (`format=metadata`). `message_count` and `participants` are computed from the returned thread's messages; `last_date` is the most recent message's `Date` header.

For `max_results=25` (default), this is ~26 API calls per list invocation, completing in well under a second. Acceptable for an agent loop. If this turns out to be a problem, a `detail` flag can be added later to allow ID-only listing.

### Body handling

Gmail's API returns base64url payloads, often with nested `parts` for multipart messages. `_decode_body`:

1. Walks `payload.parts` recursively.
2. Prefers the first `text/plain` part found.
3. Falls back to `text/html`, stripped via stdlib `html.parser.HTMLParser` subclass that accumulates character data and collapses whitespace.
4. Returns empty string if neither is present (rare — image-only messages).

### Attachment cache

- Directory: `./tmp/gmail_attachments/` (created on first download).
- File naming: `{message_id}_{filename}` — message_id namespaces filenames so two messages with attachments named `invoice.pdf` don't collide.
- TTL: **24 hours** by default, configurable via `GMAIL_ATTACHMENT_TTL_HOURS` env var.
- Sweep: `_sweep_attachments()` runs at the start of every `gmail_download_attachment` call. It lists the directory, compares `os.stat(...).st_mtime` to `time.time() - TTL_SECONDS`, and removes expired files. Cheap — one `scandir` + a few `os.remove` at most.
- No background job. No scheduler coupling. If the tool is never called, old files linger — acceptable since the tool's the only reason they exist.

Why scoped to Gmail: Drive's download tool (`google_drive.py:252-260`) has no cleanup today. Retrofitting it is a separate decision outside this feature's scope. Confirmed by grep — no existing TTL/cleanup pattern in the project to reuse.

### Truncation behavior

- Default: `full=False`, body clipped to 8000 chars (~2000 tokens, below normal summarization needs).
- Override: `full=True` returns the full decoded body.
- Signal to agent when clipped: suffix `"...[truncated, N more chars — call with full=True]"` — tells the agent (a) it was truncated, (b) roughly how much more exists, (c) how to get it.

## Error Handling

Every tool follows the existing pattern (see `google_calendar.py`):

- Not connected → `{"status": "error", "message": "Google not connected for {email}. Use google_connect first."}`
- API error → `{"status": "error", "message": "Gmail API error: {e}"}` with the underlying exception included.
- Missing required args → `{"status": "error", "message": "..."}` before the API call.

No retries — aligned with how Drive/Sheets/Calendar tools behave. The agent can retry if it wants to.

## Skill Update

Update `skills/google-workspace-skill/SKILL.md`:

- Extend `description` frontmatter to include Gmail.
- Add a **Gmail** section under **Tools** with the six tools and a one-line purpose each.
- Add a **Gmail Usage** section covering:
  - Search query syntax examples.
  - Body truncation default + override flag.
  - Attachment TTL behavior (local cache, 24h default, swept on write).
- Update the existing **Gotchas** "Existing users must reconnect" bullet to mention Gmail alongside Calendar.
- Add a gotcha noting that Gmail API must be enabled in the Google Cloud project (mirrors the existing Calendar note).

## Tests

Add `tests/test_google_gmail.py`. Follow the pattern in `tests/test_google_oauth.py` and `tests/test_oauth_service.py` (to be inspected during plan writing). At minimum:

- `_decode_body` — plain text, HTML fallback, nested multipart, empty.
- `_truncate` — under threshold, over threshold, full override.
- `_sweep_attachments` — expired files removed, fresh files kept, missing directory handled.
- Tool shape tests with mocked httpx responses — happy path for each of the six tools.

## Files Touched

- **New:** `app/tools/google_gmail.py`, `tests/test_google_gmail.py`.
- **Edited:** `app/tools/google_oauth/device_flow.py` (SCOPES), `app/toolsets/google_workspace.py` (register tools), `skills/google-workspace-skill/SKILL.md` (docs).
- **Runtime-created:** `./tmp/gmail_attachments/`.

## Future Extension Path (write tools)

When read-only proves out and we want full management:

1. Add `gmail.send` and/or `gmail.modify` to `SCOPES`. Tell users to reconnect.
2. Add write tools to `google_gmail.py` alongside the read tools:
   - `gmail_send_message(to, subject, body, cc, bcc, thread_id)`
   - `gmail_reply(message_id, body)`
   - `gmail_modify_labels(message_id, add_labels, remove_labels)` — covers mark read/unread, archive, trash
   - `gmail_create_draft(...)`, `gmail_send_draft(draft_id)`
3. Register in `google_workspace.py` toolset.
4. Update skill doc.

No refactoring of read tools needed.
