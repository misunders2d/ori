# Agent-to-Agent (A2A) v1.0 Protocol Reference

The A2A v1.0 standard provides a "common language" for interoperability between autonomous AI agents.

## Official Specification
- **A2A Official Site**: [https://agent2agent.info](https://agent2agent.info)
- **Linux Foundation Hosting**: Transitioned to enterprise-ready standard in 2025.

## Communication Patterns

### 1. Discovery (The Handshake)
Before sending tasks, agents must discover each other's capabilities.
- **Agent Card**: A JSON file at `/.well-known/agent.json` (or `agent-card.json`).
- **Contains**: Skills, security requirements, and endpoints.

### 2. Task Delegation (The Core)
The primary unit of work is the **Task**.
- **Request**: Client sends a message.
- **Response**: Remote agent returns a Task ID (strictly a server-generated UUID).
- **Lifecycle**: `WORKING`, `COMPLETED`, `FAILED`, `INPUT_REQUIRED`, `AUTH_REQUIRED`, `CANCELED`, `QUEUED`.

### 3. Real-Time Streaming
- **SSE/gRPC Streams**: Enables agents to provide partial results while a task is running.
- **Use Case**: Code generation or long-form report drafting.

### 4. Push Notifications
- **Webhooks**: For tasks taking hours/days.
- **Flow**: Client provides a callback; Remote agent notifies upon milestone or completion.

## Best Practices
- **Discovery First**: Always fetch the latest Agent Card to check for skill version updates.
- **Semantic Versioning**: Prevent breaking changes by versioning individual skills inside the card.
- **Error Taxonomy**: Use `google.rpc.Status` for consistent error handling.
- **Horizontal Coordination**: A2A is for Agent-to-Agent trust, while MCP is for Agent-to-Tool access.
