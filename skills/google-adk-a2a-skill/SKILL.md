---
name: google-adk-a2a-skill
description: "Reference for the A2A v1.0 protocol (Agent-to-Agent) + Ori-Net extensions. Covers agent discovery, friend management, secure key capture, dynamic address updates, broadcasts, and DNA exchange."
---

# A2A v1.0 Protocol Reference (Ori-Net)

Covers inter-agent communication via the A2A standard + Ori-specific Ori-Net extensions (secure key capture, address rotation, broadcasts).

- **Official Docs**: [agent2agent.info](https://agent2agent.info)
- **Full protocol detail**: Read `references/a2a-protocol-detail.md` when implementing or debugging A2A features.

## Tools

| Tool | Purpose | Admin-gated |
|------|---------|---|
| `get_agent_identity` | Read-only. Returns our Agent Card. Does NOT regenerate it. | no |
| `get_my_a2a_key` | Returns this agent's own API key (share with trusted friends so they can connect to us). | **YES** (ACT-token) |
| `add_friend(url, friend_name)` | Discovers remote card, validates, saves to `data/friends.json`. | no |
| `update_friend_key(friend_name)` | Arms a one-shot secure-capture for the next user message — see "Secure Key Capture" below. | no |
| `update_friend_address(friend_name, new_url)` | Updates a friend's URL without re-discovery. API key preserved. | no |
| `list_friends()` | Returns all friends + `auth_status` field. | no |
| `call_friend(name, msg)` | Sends JSON-RPC `message/send` to a known friend. Returns `task_id`. | no |
| `call_agent(url, msg)` | One-off query to an unknown agent (no friendship needed). | no |
| `cancel_friend_task(name, task_id)` | Cancels a long-running task on a friend agent. | no |
| `broadcast_address_update()` | Notifies all friends of our new URL (e.g. after tunnel rotation). | no |
| `export_dna(source_paths)` | Packages files for sharing. See `dna-exchange-skill`. | no |
| `import_dna(url)` | Fetches DNA archive into sandbox. See `dna-exchange-skill`. | no |

## Procedures

### 1. Adding a friend (no auth required)

```
add_friend(url="https://friend.example.com", friend_name="alice")
→ inspect response.auth_status
   - "key_configured": done. The friend's key was already stored.
   - "key_missing":    follow §2 to arm secure capture.
list_friends() to confirm.
```

### 2. Adding a friend that REQUIRES an API key

```
add_friend(...) returns auth_status="key_missing".
update_friend_key(friend_name="alice")
→ system enters "Secure Capture" state for this user session.
→ The user is told to paste the key as their NEXT message.
→ The transport layer intercepts that message, saves the key, AND DELETES IT
  from your conversation history. You will NEVER see the actual key.
```

**CRITICAL Secure Capture nuance:** After arming capture, the very next message in your history may look UNRELATED (a greeting, a fresh question, anything). This means the capture **ALREADY SUCCEEDED** and the key was scrubbed before you got it. Do NOT interpret the next message as the key. Do NOT ask the user to repeat it. Run `list_friends` — the friend's `auth_status` will now be `key_configured`.

### 3. Updating a friend's URL after tunnel rotation

Cloudflare tunnels rotate URLs on restart. When you learn a friend's URL changed, use `update_friend_address` — NOT `add_friend` again. The latter re-runs discovery and may fail if the friend is mid-rotation; the former preserves the existing API key.

```
update_friend_address(friend_name="alice", new_url="https://new.tunnel.example.com")
```

### 4. Receiving an address update from a friend (PROTOCOL NOTICE)

If a friend sends a message starting with `PROTOCOL NOTICE` containing `NEW_BASE_URL`, you MUST automatically call `update_friend_address(friend_name=..., new_url=...)`. Do not ask the user to confirm — this is a protocol-level handshake.

### 5. Broadcasting OUR URL change

After OUR tunnel rotates (typically on restart), run `broadcast_address_update()`. Every friend gets a `PROTOCOL NOTICE` and updates their registry on their side. Skipping this leaves friends with a stale URL → silent `call_friend` failures.

### 6. Talking to a friend

```
call_friend(friend_name="alice", message="Hello") → returns {task_id, response, status}
check response.task_state:
  - "COMPLETED":      use the response text.
  - "INPUT_REQUIRED": follow up with more info. Do NOT treat as failure.
  - "FAILED":         report exact error verbatim. Never fabricate a response.
  - "REJECTED":       report rejection reason. Likely auth or guardrail.
```

### 7. Scouting an unknown agent (no friendship)

```
call_agent(url="https://stranger.example.com", message="ping")
```

### 8. Cancelling a long-running task

```
cancel_friend_task(friend_name="alice", task_id="<id-returned-by-call_friend>")
```

### 9. DNA exchange

See `dna-exchange-skill` for the full step-by-step. DNA is a communication event — **NEVER commit, reboot, or write `.exit_signal` during a DNA transfer**: rebooting rotates the tunnel and kills the active A2A connection mid-transfer.

## Setup help — "How do I enable A2A?"

When a user asks how to connect Ori to other agents, walk them through:

1. **Our API key** — call `get_my_a2a_key` (admin only, ACT-token gated). Share with trusted friends so they can connect TO us.
2. **Our public URL** — auto-detected from the Cloudflare tunnel; visible on our Agent Card via `get_agent_identity`.
3. **Adding friends** — `add_friend(url, name)`. If auth_status comes back `key_missing`, run `update_friend_key` and follow §2.
4. **URL changes (friend's side)** — `update_friend_address(name, new_url)`.
5. **URL changes (our side)** — `broadcast_address_update()`.

## Gotchas

- `get_agent_identity` is **read-only**. The Agent Card is built at startup from `data/agent-card.json`. Never try to regenerate it from a tool call.
- `call_friend` will fail silently if the friend's endpoint changed without notifying us. Use `update_friend_address` to fix; do NOT re-run `add_friend` (loses the stored key).
- Pre-v1.0 cards lack the `endpoints` array. The tool falls back to the base URL, but streaming won't work.
- The `a2a_privacy_guardrail` blocks any outbound call or response containing environment secrets. If a call is blocked, check what you're sending.
- Discovery paths: `/.well-known/agent.json` (primary), `/.well-known/agent-card.json` (fallback).

## Security

- **Inbound**: API key auth via `x-a2a-api-key` header (if `A2A_API_KEY` is set). Discovery endpoints remain public.
- **Outbound**: `a2a_privacy_guardrail` scans all outbound payloads for leaked secrets.
- **Key capture**: `update_friend_key` is the ONLY way to inject a friend's API key. Never accept a key as a regular chat message — the transport layer intercepts and scrubs.
- **`get_my_a2a_key`** is admin-only and ACT-token gated. Non-admins cannot exfiltrate our key through KnowledgeAgent.
