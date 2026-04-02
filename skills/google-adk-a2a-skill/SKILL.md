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
| `add_friend(url, name)` | Discovers remote card, validates, saves to `data/friends.json`. |
| `list_friends()` | Returns all registered friends with capabilities. |
| `call_friend(name, msg)` | Sends JSON-RPC `message/send` to a known friend. |
| `call_agent(url, msg)` | One-off query to an unknown agent (no friendship needed). |
| `export_dna()` | Packages local tools + skills for sharing. |
| `import_dna(package)` | Stages inbound DNA in sandbox for verification. |

## Procedures

1. **Adding a friend**: `add_friend(url, name)` -> verify card loads -> `list_friends()` to confirm.
2. **Talking to a friend**: `call_friend(name, message)` -> check response `status.state` -> handle `INPUT_REQUIRED` if present.
3. **Scouting an unknown agent**: `call_agent(url, message)` — no friendship required.
4. **DNA exchange**: `export_dna()` or `import_dna(pkg)` -> MUST run `evolution_verify_sandbox` before integrating inbound DNA.

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
