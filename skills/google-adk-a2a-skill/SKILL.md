---
name: google-adk-a2a-skill
description: "Reference for the A2A v1.0 protocol (Agent-to-Agent). Covers agent discovery, communication with friends and arbitrary agents, and DNA exchange via the Ori-Net."
---

# A2A v1.0 Protocol Reference (Ori-Net)

This skill covers the A2A protocol implementation for inter-agent communication.
- **Official Docs**: [agent2agent.info](https://agent2agent.info)

## Agent Card (v1.0 Schema)

Every A2A agent publishes a card at `GET /.well-known/agent.json`. Required fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique agent identifier |
| `name` | string | yes | Human-readable name |
| `version` | string | yes | Agent version |
| `provider` | object | yes | `{name, url?, contact?}` |
| `endpoints` | array | yes | `[{type: "json-rpc", url: "..."}]` |
| `capabilities` | object | yes | `{streaming, pushNotifications, multiTurn, extendedAgentCard}` |
| `skills` | array | no | `[{name, description, inputSchema?, outputSchema?}]` |
| `securitySchemes` | object | no | Auth method declarations |
| `security` | array | no | Required auth for callers |

**Tool**: `get_agent_identity` (read-only, does NOT regenerate the card)

## Discovery & Friendship

Finding and registering other agents for ongoing collaboration.

- **Tool**: `add_friend(url, friend_name)` — discovers the remote agent's card, validates it, extracts the A2A endpoint, and saves to `data/friends.json`.
- **Tool**: `list_friends()` — returns all registered friends with their capabilities.
- **Discovery paths**: `/.well-known/agent.json`, `/.well-known/agent-card.json`
- **Compatibility**: Tolerates pre-v1.0 cards that lack `endpoints` (falls back to base URL).

## Communication Patterns (v1.0)

### 1. Task Delegation (Request-Response)
- **Tool**: `call_friend(friend_name, message)` — sends a JSON-RPC `message/send` request.
- **Task IDs**: Strictly server-generated UUIDs.
- **Flow**: Client sends `SendMessage` -> Remote acknowledges with `Task` -> Client polls `GetTask`.

### 2. Streaming (Real-Time Progress)
- Used for tasks that generate long content.
- **Mechanism**: Server-Sent Events (SSE) or gRPC Streams.
- **Tool**: `call_agent(url, message)` with streaming enabled.

### 3. Push Notifications (Long-Running)
- For tasks taking hours/days.
- **Mechanism**: Secure callback URL provided by the Client.

### Protocol details
- **Transport**: JSON-RPC 2.0 over HTTP POST
- **Method**: `message/send`
- **Message format**: `{role: "user", parts: [{text: "..."}]}`
- **Response**: A2A Task object with `id`, `status.state`, `artifacts`, `messages`
- **Errors**: Standardized using `google.rpc.Status` taxonomy.

### Task states (A2A v1.0)
`QUEUED` | `WORKING` | `COMPLETED` | `FAILED` | `CANCELED` | `REJECTED` | `INPUT_REQUIRED` | `AUTH_REQUIRED`

## Security

- **Inbound**: API key auth via `x-a2a-api-key` header (if `A2A_API_KEY` env var is set). Discovery endpoints remain public.
- **Outbound**: The `a2a_privacy_guardrail` blocks any call or response containing environment secrets.
- **securitySchemes**: Declared in the Agent Card so callers know what auth is required.

## DNA Exchange (Ori-specific extension)

Sharing technical improvements between Ori instances. **Not part of the A2A v1.0 standard.**

- **Export**: `export_dna()` — packages local `app/tools/*.py` and `skills/*/SKILL.md`.
- **Import**: `import_dna(package)` — stages inbound DNA in `data/sandbox/`.
- **Verification**: Inbound DNA MUST be verified with `evolution_verify_sandbox` before integration.

## Best Practices

- **Read-only identity**: Never regenerate the Agent Card from a tool call. It is built at startup.
- **Prefer friends for repeat contacts**: Use `add_friend` for agents you'll communicate with regularly.
- **Use `call_agent` for scouting**: One-off queries to unknown agents don't require friendship.
- **Semantic Versioning**: Use versioning in Agent Cards to prevent breaking changes.
- **Always check task state**: A response with `state: "INPUT_REQUIRED"` means the remote agent needs more info.
