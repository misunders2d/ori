# A2A Protocol

How Ori talks to other Ori instances (and any other A2A v1.0–compliant agent). This document describes both the **current** wire shape and the **target** shape after Phase 4 of the hardening plan.

> Current code: `app/a2a_server.py` (server), `app/tools/a2a.py` (outbound client + friend management).

---

## 1. Server

The root agent is wrapped with ADK's `to_a2a()` helper, exposing JSON-RPC endpoints over HTTP.

- Bootstrap: `app/a2a_server.py:create_a2a_app()` (lines 281–330)
- Mounted in `run_bot.py:209-233`
- Bind host: `0.0.0.0`. Port: env `A2A_PORT`, default `8000`.
- Endpoints exposed by ADK: `message/send`, `tasks/get`, `tasks/cancel`.

The server refuses to start if `A2A_API_KEY` is not set (`app/a2a_server.py:309-319`) — there is no anonymous fallback.

### Discovery endpoints (public, no auth)

| Path | Returns |
|---|---|
| `/.well-known/agent.json` | v1.0 Agent Card |
| `/.well-known/agent-card.json` | same, alias |

These are exempted by the middleware (`app/a2a_server.py:13-14`). Anyone can read the card to discover this agent's identity, capabilities, and required security scheme.

### Authenticated endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jsonrpc` (ADK-managed) | JSON-RPC `message/send` / `tasks/get` / `tasks/cancel` |
| `POST` | `/a2a/address-update` | Peer broadcasts a new `base_url` (re-pinging friends after IP change) |
| `GET` | `/dna/{filename}` | Out-of-band DNA artifact (`.tar.gz`) download |

All require the `x-a2a-api-key` header (`app/a2a_server.py:22-63`).

---

## 2. Authentication

Single header-based scheme: `x-a2a-api-key: <this_agent_api_key>`.

- **Server-side validation**: `A2AApiKeyMiddleware` (`app/a2a_server.py:22-63`) compares against `os.environ["A2A_API_KEY"]`. Mismatch → 401 JSON-RPC error.
- **Outbound**: `_a2a_headers(api_key)` (`app/tools/a2a.py:243-247`) injects the header on every outbound call.

There is **no token rotation** today — keys are long-lived per agent. Rotate by overwriting `A2A_API_KEY` in the vault, restarting, and re-broadcasting via `broadcast_address_update` (which doubles as a re-handshake).

---

## 3. Friends

Two files on disk:

| File | Content |
|---|---|
| `data/friends.json` | Public-facing friend metadata: nickname → `{base_url, card, security: [...], last_seen, ...}` |
| `data/a2a_keys.json` | Private secret store: nickname → `{api_key}` (git-ignored) |

Lifecycle:

1. **`add_friend(nickname, base_url)`** (`app/tools/a2a.py:100-169`) — fetches their Agent Card, inspects required `security[]` schemes, saves metadata. Returns `{"status": "key_missing"}` if the friend needs an API key but we don't have one for them.
2. **`update_friend_key(nickname)`** — arms a secure-capture flow; the user's next message is intercepted at the transport layer (`app/secure_config.py:84-114`) and stored to `data/a2a_keys.json` without going through the agent.
3. **`call_friend(nickname, message, attachments?)`** — looks up base_url + key, sends.
4. **`broadcast_address_update()`** — sends this agent's current `A2A_BASE_URL` to every friend. Used after IP change / DNS update / restart.

---

## 4. Message shape

### Current (text-only)

```json
{
  "jsonrpc": "2.0",
  "id": "<uuid>",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"text": "<message body>"}]
    },
    "blocking": true
  }
}
```

That's it. The outbound builder at `app/tools/a2a.py:262-266` writes `parts: [{"text": message_text}]` — no inline data, no file references. If the caller wants to share an image, the workaround today is to DNA-export the file separately and reference the URL inside the text.

### Target (Phase 4 of the hardening plan)

Parts become a discriminated union:

```json
"parts": [
  {"kind": "text", "text": "..."},
  {
    "kind": "inline_data",
    "mime_type": "image/png",
    "data": "<base64-encoded bytes>"
  },
  {
    "kind": "file_ref",
    "mime_type": "application/octet-stream",
    "name": "<uuid>",
    "size_bytes": 12345678,
    "scratchpad": "_a2a_outbound"
  }
]
```

Encoding rules (outbound, `_send_a2a_message`):

1. For each attachment, read bytes from disk-or-bytes input.
2. If total payload (text + base64 attachments) ≤ `A2A_INLINE_LIMIT` (default **5 MB**): emit as `inline_data` parts.
3. Otherwise: write the attachment to `tmp/scratchpads/_a2a_outbound/<uuid>.bin`, replace the part with a `file_ref`. The receiving side fetches via `GET https://<peer.base_url>/dna/<uuid>` with the same `x-a2a-api-key`.

Decoding rules (inbound, server pre-processor at `app/a2a_server.py`):

- `kind: inline_data` → base64-decode, write to `tmp/uploads/`, hand to `app/app_utils/file_convert.py:prepare_for_llm`.
- `kind: file_ref` → authenticated fetch, then same as `inline_data`.

### Size caps (Phase 4)

| Knob | Default | Behaviour at limit |
|---|---|---|
| `A2A_INLINE_LIMIT` | 5 MB | Outbound: spill to `file_ref` |
| `A2A_INBOUND_MAX_BYTES` | 20 MB | Inbound: hard reject (HTTP 413) |
| `_SUPPORTED_INLINE_MIMES` | see `app/app_utils/file_convert.py:80-109` | Reject non-allowlisted MIMEs |

---

## 5. Caller-ID propagation (multi-hop)

Today an inbound A2A request arrives at the root agent with `user_id` set to whatever the peer used (typically the peer's agent ID). If you call ori-A → ori-B → ori-C, by the time you reach ori-C, the original human's identity is lost.

Existing mechanism (used between Coordinator and its sub-agents for group sessions): a hidden tag `[__caller_id:<id>__]` inside the message text, extracted by `state_setter` (`app/callbacks/guardrails.py:787`).

**Phase 4 extends this to the A2A wire**:

- Outbound: `_send_a2a_message` injects header `x-a2a-caller-id: <original_user_id>`. If we are ourselves an intermediate hop, we forward the header rather than overwrite.
- Inbound: middleware reads the header (if present), seeds `state["actual_caller_id"]` before the workflow runs. `state_setter` picks it up identically to the existing in-tree tag flow.

---

## 6. DNA exchange (out-of-band)

Used to share whole evolutions (tool + skill + tests bundles) between trusted friends. The agent runs:

- `export_dna(<name>)` — packages files into `data/dna_exports/<name>.tar.gz`, returns a download URL `<this_base>/dna/<name>.tar.gz`.
- `import_dna(<friend>, <url>)` — fetches, validates (path-traversal block, secret scan), stages to sandbox. **Cannot apply directly** — the agent must call `evolution_verify_sandbox` and then go through the normal commit flow.

Hardening to add (tracked in the Phase 5 evolution-audit work):

- Add a max size limit before extraction (current code extracts unbounded).
- Replace the shallow regex secret scanner (`app/tools/a2a.py:742-774`) with an entropy + denylist + known-pattern approach.

---

## 7. Address-update endpoint

`POST /a2a/address-update`
```json
{"sender_name": "<their_nickname_as_known_to_us>", "new_base_url": "https://..."}
```

Updates `friends.json` so the next outbound call uses the new address.

Hardening to add (tracked in Phase 4):

- Validate `new_base_url` scheme (http/https only) and reject loopback/private IPs unless `A2A_ALLOW_LAN=true`.
- Rate-limit per friend (current code accepts unlimited updates).
- Re-fetch the Agent Card after the update and confirm `card.id` matches what we have on file — defends against an attacker who has the api_key but is impersonating the peer.

---

## 8. Testing

Current coverage: `tests/test_agent_card.py` (card schema validation only).

After Phase 4, `tests/test_a2a_media.py` should cover:

- Round-trip 1 MB PNG (inline_data path).
- Round-trip 50 MB blob (file_ref spillover + fetch).
- Inbound payload above `A2A_INBOUND_MAX_BYTES` → 413 reject.
- SSRF rejection on `address-update` with loopback URL when `A2A_ALLOW_LAN=false`.
- Multi-hop caller_id: A → B → C, ori-C's session state has the original user_id from A.

---

## 9. Tool reference (outbound side)

See the auto-generated `docs/TOOLS.md` for the full list; the A2A-relevant ones:

| Tool | Purpose |
|---|---|
| `get_agent_identity` | Read our own Agent Card (for the admin to copy to a friend) |
| `get_my_a2a_key` | Reveal our `A2A_API_KEY` so the admin can share it with a trusted friend |
| `add_friend` | Discover + register a friend by `base_url` |
| `update_friend_key` | Capture a friend's API key via secure flow (key never reaches the agent) |
| `list_friends` | Enumerate registered friends |
| `call_friend` | Send a message to a known friend (blocking by default; falls back to non-blocking on transient errors) |
| `call_agent` | One-off send to an arbitrary base_url (api_key optional) |
| `cancel_friend_task` | Cancel a running task on a friend |
| `perform_a2a_broadcast` / `broadcast_address_update` | Push our current `base_url` to every friend |
| `update_friend_address` | Manually edit a friend's stored `base_url` |
| `export_dna` / `import_dna` | DNA exchange (see §6) |
