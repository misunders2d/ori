---
name: agent-protocol-skill
description: "Decision tree and implementation patterns for AI agent protocols (MCP, A2A, UCP, A2UI). Use this when adding new data connectors, peer communication, commerce features, or user interfaces."
---

# Agent Protocol Decision Tree

When adding a new external capability, use this guide to choose the correct protocol.

## Choose Your Protocol

| Goal | Protocol | Tool / Component |
|------|----------|------------------|
| **Connect to Data/APIs** | **MCP** (Model Context Protocol) | `McpToolset`, `ToolboxToolset` |
| **Talk to Peer Agents** | **A2A** (Agent-to-Agent) | `RemoteA2aAgent`, `to_a2a` |
| **Buy/Sell/Checkout** | **UCP** (Universal Commerce) | `ucp_sdk` (REST based) |
| **Render UI/Dashboards** | **A2UI** / **AG-UI** | `AG-UI` components |

For detailed implementation of each protocol, read the corresponding file in `references/`:
- `references/mcp.md` — when integrating a new data source or API via MCP.
- `references/a2a.md` — when implementing A2A communication features.
- `references/ucp.md` — when adding commerce/checkout flows.
- `references/ui.md` — when building agent-rendered UI components.
- `references/links.md` — official documentation URLs for all protocols.

## Default Recommendations

- **For most data integrations**: Use MCP. Single responsibility — one server per domain.
- **For agent collaboration**: Use A2A v1.0. Discovery first — always fetch the remote Agent Card.
- **For UI**: Use AG-UI with declarative JSON. Never send raw JavaScript.
- **For commerce**: UCP only if there's a real checkout flow. Don't over-engineer.

## Security Mandate

- **No hardcoded credentials**: Never put secrets in protocol tool code. Use `configure_integration` for secure key capture.
- **Privacy guardrails**: `a2a_privacy_guardrail` MUST be attached to all A2A/MCP toolsets. It blocks outbound payloads containing environment secrets.
- **MCP server isolation**: Run local MCP servers in restricted environments with explicit file system allowlists.

## Gotchas

- **MCP servers are stateless bridges**: They must be read-only. Any mutating MCP tool must be replaced with a native tool in `app/tools/`.
- **A2A discovery has two paths**: `/.well-known/agent.json` (standard) and `/.well-known/agent-card.json` (legacy). Check both.
- **UCP requires Merchant of Record**: The business must retain control of the customer relationship. Don't build UCP flows where the agent becomes the merchant.
- **AG-UI is declarative only**: Send JSON component descriptions. The client-side renderer translates to native UI. If you send executable code, the guardrails should block it.
- **A2A vs MCP confusion**: A2A is for agent-to-agent trust (peer communication). MCP is for agent-to-tool access (data sources). They solve different problems and are often used together.
