# Telegram skills

Three Telegram capabilities layered on top of the existing
`interfaces/telegram_poller.py` polling loop:

1. Send a message to a group / channel from the agent.
2. Extract `chat_id` from a forwarded message and persist it under a
   user-chosen alias.
3. Re-forward bot-generated (or user-saved) media via the Telegram
   `file_id` cache — no byte roundtrip, no re-upload.

Design comes from the proposal at
`tmp/proposals/telegram-skills-improvements.md` (v3, reviewer-approved
2026-05-20). This document is the canonical reference once the proposal
file is deleted; until then they are kept in sync.

> **Status:** rolling implementation. Each section below lands as a separate
> commit per `§11 Implementation order` in the proposal. Section headers note
> which slice introduced the symbol.

---

## Capability layer (slice 1)

`app/core/capabilities.py` is a per-user capability store that sits on top
of the binary whitelist (`app/core/whitelist.py`). The whitelist gates
inbound traffic; capabilities gate privileged outbound operations.

Canonical capabilities (v1):

| Capability | Required by |
|---|---|
| `send_to_groups` | `telegram_send_to_chat` when the resolved target is a group / channel / supergroup |
| `manage_aliases` | `telegram_save_alias`, `telegram_save_last_forward_alias`, `telegram_delete_alias` |
| `forward_files`  | `telegram_forward` when the resolved target is not a DM |

Listing and resolving aliases are self-scope and need no capability.

**Admin implicit grant.** Any `user_id` in the `ADMIN_USER_IDS` env var
holds every canonical capability. Pattern matches the whitelist's
admin-bypass (`app/core/whitelist.py:26-33`).

**API (`app/core/capabilities.py`).** All entry points are async.

```python
async def has_capability(user_id: str, capability: str) -> bool
async def grant(user_id: str, capability: str) -> None
async def revoke(user_id: str, capability: str) -> None
async def list_for(user_id: str) -> list[str]
async def reload() -> None
async def all_users() -> list[str]
```

Unknown capability names raise `ValueError` so a typo cannot silently
deny.

**Persistence.** JSON at `data/capabilities.json`:

```json
{
  "tg_111": ["send_to_groups", "manage_aliases"],
  "tg_222": ["forward_files"]
}
```

Disk I/O is wrapped in `asyncio.to_thread` so the event loop is never
blocked (Law 5). Writes are serialized by a module-level `asyncio.Lock`.

**Atomic write contract.**

1. Acquire the module-level lock.
2. Write to `capabilities.json.tmp` in the same directory.
3. `f.flush()` then `os.fsync(fd)` to push bytes to disk.
4. `os.replace(tmp, final)` — atomic on POSIX.
5. Release the lock in `finally`.

The on-disk file is never partially written. If `os.replace` raises, the
original is left intact; tests confirm a subsequent `grant` can still
proceed after the lock is released.

**Corrupt-load rule.**

- **First load with corrupt file** → cache becomes `{}`, `logger.error(...)`,
  and the on-disk file is left untouched.
- **Subsequent reload with corrupt file** → stale in-memory cache is
  preserved, `logger.error(...)`, file left untouched.
- A successful `grant` / `revoke` after a corrupt load atomically replaces
  the bad file. Operators who want the bad file preserved should back it
  up before issuing a mutation.

The store treats the following as corrupt: invalid JSON, top-level non-dict,
row whose value is not a list of strings, or non-string capability entries.

---

## Persistence layer (slice 2)

`app/core/telegram_store.py` is the async SQLite layer for two distinct
concerns + an in-memory cache. Single DB file: `data/telegram_skills.db`.

### Tables

```sql
CREATE TABLE IF NOT EXISTS chat_aliases (
    owner_user_id TEXT NOT NULL,
    alias         TEXT NOT NULL,           -- case-folded by writer
    chat_id       INTEGER NOT NULL,
    chat_type     TEXT NOT NULL,           -- 'channel'|'supergroup'|'group'
    title         TEXT,
    username      TEXT,
    created_at    TEXT NOT NULL,           -- ISO-8601 UTC
    last_used_at  TEXT,
    PRIMARY KEY (owner_user_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_chat_aliases_owner ON chat_aliases(owner_user_id);

CREATE TABLE IF NOT EXISTS outbound_files (
    file_ref          TEXT NOT NULL,       -- case-folded by writer
    owner_user_id     TEXT NOT NULL,
    file_id           TEXT NOT NULL,       -- Telegram-side ID; reusable across chats
    file_type         TEXT NOT NULL,       -- 'photo'|'document'|'audio'|'video'|'voice'|'video_note'
    filename          TEXT,
    mime_type         TEXT,
    source_chat_id    INTEGER,             -- copyMessage fallback
    source_message_id INTEGER,
    caption           TEXT,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, file_ref)
);
CREATE INDEX IF NOT EXISTS idx_outbound_files_expires ON outbound_files(expires_at);
```

### I/O contract (Law 5)

- All disk operations use `aiosqlite`.
- Schema is created lazily via `_ensure_schema()` on first call. No
  import-time DB writes.
- Each operation opens a short-lived `aiosqlite.connect(...)` connection
  and closes via the async context manager. Concurrent operations are
  serialized by SQLite itself; schema init is double-checked under a
  module-level `asyncio.Lock`.

### TTL semantics

- File cache default: `TELEGRAM_FILE_CACHE_TTL_HOURS` env var (default
  168 hours = 7 days). Bad / empty values fall back to default and emit a
  WARNING.
- `get_file(..., slide=True)` (default) refreshes `expires_at` forward by
  the configured TTL on every successful hit.
- Expired rows are eagerly pruned on lookup; `prune_expired_files()` is
  available for batch sweeping.
- Last-forward cache: 5 minutes, in-memory only, lost on restart.

### Validation

- `chat_type` for aliases is restricted to `channel` / `supergroup` /
  `group`. DM aliases raise `ValueError`; the cross-platform roster
  (`app/core/roster.py`) is the canonical namespace for DM targets.
- `file_type` for outbound files is restricted to the six Bot API send
  methods we re-emit. Sticker is intentionally NOT in the enum (v1 scope
  per proposal §8).
- Aliases and file_refs are case-folded on write and lookup; `Engineering`
  and `engineering` are the same row.

### API (all async unless noted)

```python
# Aliases
async def save_alias(owner_user_id, alias, chat_id, chat_type, title=None, username=None) -> None
async def list_aliases(owner_user_id) -> list[dict]
async def resolve_alias(owner_user_id, alias) -> dict | None    # slides last_used_at
async def delete_alias(owner_user_id, alias) -> bool

# Files
async def put_file(owner_user_id, file_ref, file_id, file_type, *,
                   filename=None, mime_type=None,
                   source_chat_id=None, source_message_id=None,
                   caption=None, ttl=None) -> dict
async def get_file(owner_user_id, file_ref, *, slide=True) -> dict | None
async def list_files(owner_user_id) -> list[dict]
async def delete_file(owner_user_id, file_ref) -> bool
async def prune_expired_files() -> int

# Last-forward cache (sync, in-memory)
def stash_forward(session_id, chat_id, chat_type, title=None, username=None) -> None
def peek_forward(session_id) -> ForwardCapture | None
def pop_forward(session_id) -> ForwardCapture | None
```

---

## Transport ABC strict variants (slice 3)

Adapter-level support for tool-grade error surfacing. The existing
`send_message` / `send_media` methods log non-200 responses and return
`None` — fine for the poller's fire-and-forget delivery, useless for an
agent tool that must surface the Telegram error verbatim (Law 6).

Three new abstract methods on `TransportAdapter` (`app/core/transport.py`):

```python
async def send_text_strict(
    self, target_id: str | int, text: str
) -> dict: ...

async def send_media_strict(
    self,
    target_id: str | int,
    data: bytes | None,
    mime_type: str,
    caption: str = "",
    *,
    file_id: str | None = None,
    file_type: str | None = None,
    file_ref: str | None = None,
    owner_user_id: str | None = None,
    file_path: str | None = None,
) -> dict: ...

async def copy_message_strict(
    self, target_id: str | int, from_chat_id: int, message_id: int
) -> dict: ...
```

**Return contract.** Adapters MUST return one of:

```python
{"ok": True, "message_id": int, ...}
{"ok": False, "error_code": int, "description": str}
```

They MUST NOT raise on a platform-level non-200 (that path is reserved
for programmer errors / network failures). They MUST NOT return `None`.

**send_media_strict modes.**
1. **Re-send by cached file_id** — caller passes `file_id` + `file_type`;
   adapter picks the Bot API method from `file_type`
   (`sendPhoto` / `sendDocument` / `sendAudio` / `sendVideo` /
   `sendVoice` / `sendVideoNote`). MIME alone cannot disambiguate voice
   from audio or video_note from video.
2. **Bytes upload** — caller passes `data` + `mime_type`; adapter
   picks the method via MIME prefix mapping (existing behavior).
   `file_type` may be supplied to override the mapping.

On success, if `file_ref` (explicit) or `file_path` (auto-derive) plus a
non-empty `owner_user_id` are supplied, the adapter writes the result
into `outbound_files` for later forwarding.

**v1 implementation status.**
- `TelegramAdapter` — slice-3 ships stubs that raise
  `NotImplementedError`. Real impls land in slice 4.
- `SlackAdapter` — raises `NotImplementedError` (no `file_id`-equivalent
  in the Slack Web API; cross-chat file forwarding is Telegram-only per
  proposal §8).
- Both subclasses remain instantiable (verified by
  `tests/test_transport_strict_abc.py`) so the live poller does not break.

---

## TelegramAdapter strict impls (slice 4)

`TelegramAdapter` (`interfaces/telegram_poller.py`) now implements all
three strict variants. The fire-and-forget `send_message` / `send_media`
paths are unchanged — the poller's outbound delivery loop still uses
them. New code (tools, /forward command, etc.) routes through the
strict variants.

### `_post(method, *, json=None, data=None, files=None) -> dict`

Private helper. POSTs to the Bot API and returns the parsed response.
Telegram's body is always `{"ok": bool, ...}`. Transport errors and
non-JSON responses are wrapped into the same shape so the layer above
can branch on `body["ok"]` without try/except boilerplate. NEVER raises
on a platform 4xx.

### `send_text_strict(target_id, text)`

- Re-uses `_scrub_secrets` and `_escape_urls_for_telegram_md`
  (production proof 2026-05-12 Google OAuth bug).
- Rejects text > 4096 chars pre-flight; strict callers chunk before
  sending.
- Tries Markdown parse_mode first; on Telegram-side rejection retries
  in plain text (matches the existing `send_message` fallback).
- Returns `{"ok": True, "message_id", "chat_id"}` or
  `{"ok": False, "error_code", "description"}`.

### `send_media_strict(...)`

Two modes:

**1. Re-send by cached file_id.** Requires `file_type` (proposal #1 —
voice/audio and video_note/video cannot be disambiguated by MIME alone).
JSON POST to the matching Bot API method with the `file_id` value under
the matching field.

**2. Bytes upload.** When `file_id is None`. Picks the method via the
MIME-prefix mapping in `_mime_to_file_type` (image→photo, audio→audio,
video→video, else→document). `file_type` may override that mapping.
Multipart upload with caption (secrets-scrubbed).

On success, extracts the outgoing `file_id` per type
(`photo` is a `[size...]` list — pick the largest; everything else is a
single object). When `file_ref` (explicit) or `file_path` (auto-derive
case-folded basename, sans extension) is supplied AND `owner_user_id`
is non-empty, writes an `outbound_files` row via
`telegram_store.put_file` so the file can be re-forwarded later. Cache
failure is non-fatal (logged at EXCEPTION; the send already succeeded).

### `copy_message_strict(target_id, from_chat_id, message_id)`

Thin wrapper over Bot API `copyMessage`. Used as the fallback path when
re-sending by `file_id` is rejected by Telegram with a "wrong file
identifier" or similar error (slice 7 forward flow).

### Per-type file_id extraction map

| `file_type` | Bot API method | Response shape |
|---|---|---|
| `photo` | `sendPhoto` | `result.photo[-1].file_id` (largest size) |
| `document` | `sendDocument` | `result.document.file_id` |
| `audio` | `sendAudio` | `result.audio.file_id` |
| `video` | `sendVideo` | `result.video.file_id` |
| `voice` | `sendVoice` | `result.voice.file_id` |
| `video_note` | `sendVideoNote` | `result.video_note.file_id` |

`sticker` intentionally NOT in this table (v1 scope per proposal §8).

### Tests

`tests/test_telegram_adapter_strict.py` — 23 cases:
- `send_text_strict`: success, markdown fallback, verbatim error
  surfacing, oversize-rejected pre-flight, HTTP transport-error wrap.
- `send_media_strict` by file_id: parametrized routing for all six
  allowed types; voice-vs-audio disambiguation; video_note-vs-video
  disambiguation; refusal when `file_id` is set but `file_type` is not;
  refusal when neither `data` nor `file_id` is provided.
- `send_media_strict` bytes upload: MIME-prefix fallback to photo;
  unknown-MIME fallback to document; explicit `file_type` overrides MIME.
- Cache write side-effect: row appears in `outbound_files` when
  `file_ref` + `owner_user_id` are passed; auto-derive from `file_path`;
  no cache row when `owner_user_id` is missing.
- `copy_message_strict`: success and verbatim-failure paths.

---

## media_items.file_path threading (slice 5)

`extract_agent_response` (`app/core/agent_executor.py`) emits an
`AgentResponse(media_items=[...])` for the transport pollers to ship.
Each item used to be `{"data": bytes, "mime_type": str}`. Slice 5
widens the dict shape with a third optional key:

```python
{
    "data": bytes,
    "mime_type": str,
    "file_path": str | None,
}
```

- Function-response branch (tool returned `{"file_path": "..."}`):
  `file_path` is the absolute path of the source file.
- Inline-data branch with the `__contract_file:<abs path>` display_name
  marker (set by `file_attachment_inject`): `file_path` is the absolute
  marker path.
- User-uploaded inline_data (no marker): `file_path = None`. The
  transport MUST NOT write a cache row for somebody else's upload.

Slice 6 reads this key in the Telegram poller's delivery loop and
threads it into `send_media_strict(file_path=..., owner_user_id=...)`
so bot-generated charts auto-populate the outbound-files cache. Slack
and any other consumer can keep using `item["data"]` /
`item["mime_type"]` exactly as before — the new key is additive.

Tests (`tests/test_executor.py`, 3 new cases on top of the existing 5):
- function-response branch emits absolute `file_path`.
- inline-data marker branch emits absolute marker path.
- inline-data without marker (user upload) → `file_path` is `None`.

---

## Poller delivery loop auto-derive (slice 6)

The Telegram poller's `_process_and_send` (in
`interfaces/telegram_poller.py`) historically shipped media via the
best-effort `adapter.send_media(...)`. Slice 6 switches to the strict
variant and threads the new `file_path` key so bot-generated files
(charts, presentations, exports) auto-populate `outbound_files` without
the agent having to call any cache tool.

### Module-level helper: `_deliver_media_items`

```python
async def _deliver_media_items(
    adapter: TelegramAdapter,
    chat_id: str | int,
    owner_user_id: str | None,
    media_items: list[dict],
) -> list[dict]:
    ...
```

For each item in `media_items`:
- skip malformed entries (non-dict, missing/wrong-type `data`).
- call `adapter.send_media_strict(chat_id, data, mime_type,
  file_path=item.get("file_path"), owner_user_id=owner_user_id)`.
- if `result["ok"]` is False: log at ERROR with Telegram's `description`
  verbatim (Law 6); DO NOT raise; continue with the next item.

Extracting the helper makes the wiring testable in isolation (no need
to drive the full `poll_telegram` loop in tests).

### Cache flow end-to-end

1. Tool generates a chart, returns `{"file_path": "/tmp/q1_revenue.png", "status": "success"}`.
2. `extract_agent_response` walks the function_response and inline_data
   parts; emits `media_items=[{"data": ..., "mime_type": "image/png",
   "file_path": "/tmp/q1_revenue.png"}]` (slice 5).
3. Poller's `_process_and_send` calls `_deliver_media_items(adapter,
   chat_id, user_id, response.media_items)`.
4. Adapter's `send_media_strict` (slice 4) uploads bytes, extracts the
   outbound `file_id`, and writes a row to `outbound_files` keyed by
   `owner_user_id=tg_111`, `file_ref="q1_revenue"` (case-folded
   basename, sans extension), `file_type="photo"`.
5. Later the user / agent can `/forward q1_revenue to <alias>` and the
   slice-7 tool will resolve the alias, look up the cache row, and
   re-send via `file_id` (no byte upload).

### Tests (`tests/test_telegram_autoderive_file_ref.py`, 6 cases)

- helper threads `file_path` + `owner_user_id` for every item.
- helper uses strict, not best-effort (regression pin).
- helper logs each failure and continues with the next item.
- helper skips non-dict / missing-data items defensively.
- end-to-end integration with a fake httpx client: real
  `TelegramAdapter.send_media_strict` populates the cache with the
  auto-derived ref.
- `file_path=None` items reach the adapter with the same value so no
  cache row is written.

---

## Agent-callable tools (slice 7)

`app/tools/telegram.py` now exposes the full agent-facing surface for
the three new features. Every signature carries
`tool_context: ToolContext = None`. Caller identity is read from
`tool_context.state["user_id"]`; missing context returns
`{"status": "error", "message": "Missing caller user_id in
tool_context.state — refusing to proceed..."}` rather than silently
permitting the operation.

### Capability matrix (locked in v3)

| Tool | Capability |
|---|---|
| `telegram_send_dm` | — (existing) |
| `telegram_send_to_chat` | `send_to_groups` (only when target is non-DM) |
| `telegram_save_alias` | `manage_aliases` |
| `telegram_save_last_forward_alias` | `manage_aliases` |
| `telegram_list_aliases` | — (self-scope) |
| `telegram_delete_alias` | `manage_aliases` |
| `telegram_resolve_alias` | — (self-scope) |
| `telegram_forward` | `forward_files` (only when target is non-DM) |
| `telegram_list_cached_files` | — (self-scope) |
| `telegram_grant_capability` | admin (ACT+TOTP via guardrail — slice 10) |
| `telegram_revoke_capability` | admin (ACT+TOTP via guardrail — slice 10) |
| `telegram_list_capabilities` | self-scope; cross-user → admin inline check |

### Target resolution

`_resolve_target(caller_user_id, target)` recognizes three forms:

- `tg_-1001234` / `tg_111` — prefixed canonical id; chat_type unknown.
- `-1001234` / `111` — raw signed/unsigned int; chat_type unknown.
- any other token — alias lookup scoped to `caller_user_id`.

`_is_non_dm(chat_id, chat_type)` is the cap-check classifier:
- alias-resolved row carries `chat_type` → channel/supergroup/group are
  always non-DM.
- raw target with no chat_type → negative chat_id is non-DM (Telegram
  convention); positive chat_id is DM-shaped per proposal §12 v2-c (no
  cap, fail loud at Telegram).

### Forward flow

1. Resolve target.
2. Capability check (`forward_files` for non-DM).
3. `telegram_store.get_file(caller, file_ref)` (eager-prunes expired
   rows, slides `expires_at` forward on hit).
4. `adapter.send_media_strict(chat_id, data=None, mime_type=row.mime_type,
   file_id=row.file_id, file_type=row.file_type, caption=row.caption)`.
5. On `ok`: return `status=success, method="file_id"`.
6. On Telegram-side error AND row has `source_chat_id`/`source_message_id`:
   try `adapter.copy_message_strict(target, source_chat, source_msg)`.
7. On combined failure: return error with both descriptions.
8. On error without source ids: return error suggesting re-upload.

### Tests (4 files, 54 cases)

- `tests/test_telegram_send_to_chat.py` — 10 cases. happy-path with
  alias + cap; cap-missing blocks before adapter call; bot-not-member
  surfaces Telegram description verbatim; raw negative chat_id (with /
  without cap); positive raw chat_id skips cap (v2-c); `tg_-`-prefixed
  target; unknown alias; missing caller user_id refused; admin implicit
  bypass.
- `tests/test_telegram_forward.py` — 15 cases. file_id resend +
  parametrized per-file_type routing for all 6 types;
  voice/audio + video_note/video disambiguation; cache miss; expired;
  wrong-owner isolation; copyMessage fallback success; combined-failure
  error; no-source-ids error suggesting re-upload; cap gating; positive
  raw target skips cap; admin implicit bypass.
- `tests/test_telegram_alias_tools.py` — 15 cases. save/list/delete/
  resolve happy paths; cap matrix enforcement; DM-type rejection;
  chat_id type validation; owner-scoped lists isolate; save_last_forward
  happy / no-stash / no-cap; missing-context refusal across all
  five tools.
- `tests/test_telegram_capability_tools.py` — 14 cases. grant happy /
  unknown-name error / required-fields; revoke happy / no-op;
  list-self / list-self-via-arg / cross-user blocked / admin
  cross-user / admin implicit-all; missing-context refused.

Auto-generated `docs/INDEX.md` and `docs/TOOLS.md` are refreshed by
`scripts/gen_docs.py` and committed alongside the slice.

---

## Poller short-circuits (slice 8)

Five deterministic, pre-LLM command handlers live as module-level
functions in `interfaces/telegram_poller.py`. Each returns `True` when
it consumed the message (the outer poll loop `continue`s). They run
AFTER the whitelist gate and BEFORE the agent runner is invoked.

### Order of evaluation (orchestrated by `_run_short_circuits`)

The five handlers are dispatched in a fixed order by the module-level
`_run_short_circuits(adapter, msg, text, chat_id, chat_type,
caller_user_id, session_id) -> bool`. Order is load-bearing —
forward-extract must run BEFORE any text/empty-message guard because a
forwarded channel post is often pure metadata (no text, no file).

1. `_handle_forward_extract` — DM-only. Detects `forward_origin` /
   `forward_from_chat` / `forward_from` metadata, replies with the
   captured chat_id + title + username, and stashes
   group/channel/supergroup forwards into
   `telegram_store.last_forward_cache` (5-min TTL) so `/alias save
   <name>` can pop it. DM-from-user and hidden_user forwards are
   acknowledged but NOT stashed (DM aliases out of v1 scope).
2. `_handle_savefile_command` — caption-driven. DM-only. Triggers on
   the EXACT `/savefile` TOKEN (`/savefilex` is not consumed). Free-text
   `save as <name>` is not a trigger (proposal §4 — reviewer rejected
   the ambiguous form). Captures the inbound `file_id` for the first
   recognized file kind (photo / document / audio / video / voice /
   video_note) and writes to `outbound_files` keyed by `<name>`. Source
   chat_id + message_id are recorded for the `copyMessage` fallback.
3. `_handle_alias_command` — DM-only. `/alias save|delete|list|resolve|help`.
   - `save` / `delete` require `manage_aliases`.
   - `list` / `resolve` are self-scope and need no capability.
   - `save` pops the last-forward cache; expired or absent stash
     replies with a refresh prompt.
   - `has_capability` lookups are wrapped: store failures return a
     fail-loud reply + EXCEPTION log (Law 6) instead of propagating.
4. `_handle_forward_command` — `/forward <ref> to <alias-or-chatid>`.
   Group/DM allowed (the `forward_files` cap is enforced by
   `telegram_forward` for non-DM targets). Splits on the LAST `" to "`
   so file_refs containing that substring survive.
5. `_handle_cap_command` — `/cap grant|revoke|list|help`. Group/DM
   allowed (admin gate inline). Mutations + cross-user reads require
   `ADMIN_USER_IDS` membership (parity with `/models set`, no ACT+TOTP
   — proposal §12 v2-a). Every grant/revoke logs at INFO with the
   admin id; cross-user list ALLOWED/DENIED both log at INFO for audit
   symmetry with the LLM-tool path.

All four `/`-prefixed commands use **token-exact** matching:
`_command_token(text)` returns the first whitespace-separated token,
and the handler bails when that token is not the exact command
(`/savefile`, `/alias`, `/forward`, `/cap`). Strings like `/savefilex`,
`/aliasfoo`, `/capextra` are NOT consumed.

### Wiring

Inside `poll_telegram`'s main `for update in data["result"]:` loop,
the call lives right after roster recording and BEFORE the file-
handling / empty-message-reject block:

```python
if await _run_short_circuits(
    adapter=adapter,
    msg=msg,
    text=text,
    chat_id=chat_id,
    chat_type=chat_type,
    caller_user_id=user_id,
    session_id=session_id,
):
    continue
```

Each handler swallows `ValueError` as a user-facing reject message and
catches broader `Exception` to log + reply with the underlying error
(no silent failure — Law 6).

### Tests (4 files, 72 cases)

- `tests/test_telegram_poller_forward_extract.py` — 10. channel /
  supergroup / group forwards via modern + legacy fields; DM-from-user
  + hidden_user explanation paths; non-DM and no-metadata pass-through;
  5-min TTL; partial origin payload defensive path.
- `tests/test_telegram_alias_commands.py` — 21. all four subcommands
  + help + bare; cap gating on save/delete; save with no last-forward
  + happy + missing-name; list empty + populated; delete miss; resolve
  miss; unknown subcommand falls back to usage; `/forward` happy +
  help + bare + bad-syntax + tool-error pass-through.
- `tests/test_telegram_cap_commands.py` — 12. /cap list self
  (no-arg / via-arg-self / cross-user-denied / cross-user-admin /
  empty); /cap grant non-admin blocked; admin grant + revoke happy;
  unknown-capability rejected; missing-args usage; cross-user
  ALLOWED/DENIED audit log assertions.
- `tests/test_telegram_savefile_command.py` — 13. free-text 'save as'
  NOT consumed; no-caption pass-through; no-name usage; help usage;
  no-attachment prompt; document + photo happy; parametrized over
  audio / video / voice / video_note; duplicate overwrite; owner-scope
  isolation.

---

## TelegramSkillsToolset (slice 9)

`app/toolsets/telegram_skills.py` exposes `TelegramSkillsToolset(BaseToolset)`
— the agent-facing bundle for the twelve telegram tools. Re-exported
from `app/toolsets/__init__.py` so consumer agents can
`from app.toolsets import TelegramSkillsToolset`.

The toolset lazy-imports `app.tools.telegram` inside `get_tools()` so
constructing the toolset does NOT drag in adapter / httpx setup at
import time. Tools returned (in declaration order):

```python
[
    FunctionTool(func=telegram_send_dm),
    FunctionTool(func=telegram_send_to_chat),
    FunctionTool(func=telegram_save_alias),
    FunctionTool(func=telegram_save_last_forward_alias),
    FunctionTool(func=telegram_list_aliases),
    FunctionTool(func=telegram_delete_alias),
    FunctionTool(func=telegram_resolve_alias),
    FunctionTool(func=telegram_forward),
    FunctionTool(func=telegram_list_cached_files),
    FunctionTool(func=telegram_grant_capability),
    FunctionTool(func=telegram_revoke_capability),
    FunctionTool(func=telegram_list_capabilities),
]
```

Tests (`tests/test_telegram_skills_toolset.py`, 5 cases):
- `get_tools()` returns the exact twelve canonical tool names.
- Tool count == 12.
- Re-exported in `app.toolsets.__all__` and importable directly.
- Constructor does NOT eager-import `app.tools.telegram`.
- First `get_tools()` call populates `sys.modules` lazily.

The Coordinator mount + dropping the direct `telegram_send_dm` import
land in slice 10.

---

## (Remaining sections land with subsequent slices.)
- Slice 10 — Coordinator mount + admin-gate wiring.
