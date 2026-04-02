# A2A v1.0 Protocol Detail

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

## Communication Patterns

### 1. Task Delegation (Request-Response)
- **Tool**: `call_friend(friend_name, message)` — sends a JSON-RPC `message/send` request.
- **Task IDs**: Strictly server-generated UUIDs.
- **Flow**: Client sends `SendMessage` -> Remote acknowledges with `Task` -> Client polls `GetTask`.

### 2. Streaming (Real-Time Progress)
- For tasks generating long content.
- **Mechanism**: Server-Sent Events (SSE) or gRPC Streams.
- **Tool**: `call_agent(url, message)` with streaming enabled.

### 3. Push Notifications (Long-Running)
- For tasks taking hours/days.
- **Mechanism**: Secure callback URL provided by the Client.

### Protocol Wire Format
- **Transport**: JSON-RPC 2.0 over HTTP POST
- **Method**: `message/send`
- **Message format**: `{role: "user", parts: [{text: "..."}]}`
- **Response**: A2A Task object with `id`, `status.state`, `artifacts`, `messages`
- **Errors**: Standardized using `google.rpc.Status` taxonomy.

### Task States
`QUEUED` | `WORKING` | `COMPLETED` | `FAILED` | `CANCELED` | `REJECTED` | `INPUT_REQUIRED` | `AUTH_REQUIRED`

## DNA Exchange (Ori-specific extension)

Sharing technical improvements between Ori instances. **Not part of the A2A v1.0 standard.**

- **Export**: `export_dna()` — packages local `app/tools/*.py` and `skills/*/SKILL.md`.
- **Import**: `import_dna(package)` — stages inbound DNA in `data/sandbox/`.
- **Verification**: Inbound DNA MUST be verified with `evolution_verify_sandbox` before integration.
