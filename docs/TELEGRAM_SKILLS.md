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

## (Remaining sections land with subsequent slices.)

- Slice 3 — `TransportAdapter` strict variants (`send_text_strict`,
  `send_media_strict`, `copy_message_strict`).
- Slice 4 — `TelegramAdapter` strict impls.
- Slice 5 — `media_items` shape extension.
- Slice 6 — poller delivery loop auto-derive `file_ref`.
- Slice 7 — agent-callable tools in `app/tools/telegram.py`.
- Slice 8 — poller short-circuits (`/alias`, `/forward`, `/cap`,
  `/savefile`, forward-extract).
- Slice 9 — `TelegramSkillsToolset`.
- Slice 10 — Coordinator mount + admin-gate wiring.
