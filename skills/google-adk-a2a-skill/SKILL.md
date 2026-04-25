---
name: google-adk-a2a-skill
description: "Reference for the A2A v1.0 protocol (Agent-to-Agent). Covers agent discovery, communication with friends and arbitrary agents, and DNA exchange via the Ori-Net."
---

# A2A v1.0 Protocol Reference (Ori-Net)

Covers inter-agent communication via the A2A standard.
- **Official Docs**: [agent2agent.info](https://agent2agent.info)
- **Full protocol detail**: Read `references/a2a-protocol-detail.md` when implementing or debugging A2A features.

## Tools

| Tool | Purpose |
|------|---------|
| `get_agent_identity` | Read-only. Returns our Agent Card. Does NOT regenerate it. |
| `get_my_a2a_key` | Returns this agent's `A2A_API_KEY` for sharing with trusted friends. |
| `add_friend(url, name)` | Discovers remote card, validates, saves to `data/friends.json`. If the friend requires auth, immediately call `update_friend_key`. |
| `update_friend_key(friend_name)` | Arms secure-capture for a friend's API key. The user's NEXT message is intercepted, saved to vault, and never reaches the LLM. If the next message in your context looks unrelated (a greeting, a different command), the capture ALREADY succeeded — do NOT assume the message is the key. Use `list_friends` to verify status. |
| `update_friend_address(friend_name, new_url)` | Update a friend's URL without re-entering the API key (e.g. tunnel URL rotation). |
| `list_friends()` | Returns all registered friends with capabilities and auth status. |
| `call_friend(name, msg)` | Sends JSON-RPC `message/send` to a known friend. `msg` may be a plain string OR a `types.Content` with mixed text + binary parts. |
| `call_agent(url, msg)` | One-off query to an unknown agent (no friendship needed). |
| `cancel_friend_task(friend_name, task_id)` | Cancels a long-running task. |
| `broadcast_address_update()` | Notifies all friends when our public URL changes (e.g. tunnel restart). |
| `export_dna(source_paths)` | Packages project files for sharing. See `dna-exchange-skill`. |
| `import_dna(url)` | Fetches DNA archive into sandbox. See `dna-exchange-skill`. |

## Multimodal Content (text + binary in one message)

`call_friend` and `call_agent` accept `types.Content` for mixed payloads. Binary parts ride inline as base64 per the A2A spec.

```python
from google.genai import types
content = types.Content(
    role="user",
    parts=[
        types.Part.from_text(text="Here is the file: <manifest>"),
        types.Part.from_bytes(data=<bytes>, mime_type="application/gzip"),
    ],
)
await call_friend(friend_name, content)
```

Use this for images, audio, files, or DNA bundles. For DNA specifically, follow `dna-exchange-skill`.

## Address Broadcasting

When Ori's public URL changes (e.g. Cloudflare tunnel restart), call `broadcast_address_update` to notify all friends. They each receive a `PROTOCOL NOTICE` containing `NEW_BASE_URL`.

If you receive an inbound message that starts with `PROTOCOL NOTICE` and contains `NEW_BASE_URL`, automatically call `update_friend_address(friend_name, new_url)` — no user confirmation needed; this is house-keeping, not a privileged action.

## Task State

Every response from a remote agent has a `task_state`:

| State | Action |
|---|---|
| `COMPLETED` | Use the result. |
| `INPUT_REQUIRED` | Follow up with more info — not a failure. |
| `FAILED` / `REJECTED` | Report the error to the user. Don't retry blindly. |

## Procedures

1. **Adding a friend**: `add_friend(url, name)` -> verify card loads -> `list_friends()` to confirm.
2. **Talking to a friend**: `call_friend(name, message)` -> check response `status.state` -> handle `INPUT_REQUIRED` if present.
3. **Scouting an unknown agent**: `call_agent(url, message)` — no friendship required.
4. **DNA exchange**: Follow the `dna-exchange-skill` step-by-step procedure. DNA is a communication event — never commit or reboot.

## Gotchas

- `get_agent_identity` is **read-only**. The Agent Card is built at startup from `data/agent-card.json`. Never try to regenerate it from a tool call.
- `call_friend` will fail silently if the friend's endpoint changed. Re-run `add_friend` to refresh the card.
- A response with `state: "INPUT_REQUIRED"` means the remote agent needs more info — don't treat it as a failure.
- Pre-v1.0 cards lack the `endpoints` array. The tool falls back to the base URL, but streaming won't work.
- The `a2a_privacy_guardrail` blocks any outbound call or response containing environment secrets. If a call is blocked, check what you're sending.
- Discovery paths: `/.well-known/agent.json` (primary), `/.well-known/agent-card.json` (fallback).

## Security

- **Inbound**: API key auth via `x-a2a-api-key` header (if `A2A_API_KEY` is set). Discovery endpoints remain public.
- **Outbound**: `a2a_privacy_guardrail` scans all outbound payloads for leaked secrets.
