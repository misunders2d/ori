---
name: agent-protocol-skill
description: "Decision tree and implementation patterns for AI agent protocols (MCP, A2A, UCP, A2UI). Use this when adding new data connectors, peer communication, commerce features, or user interfaces."
---

# Agent Protocol Decision Tree

When tasked with adding a new external capability, use this guide to choose the correct standard protocol.

## 1. Choose Your Protocol

| Goal | Protocol | Tool / Component |
|------|----------|------------------|
| **Connect to Data/APIs** | **MCP** (Model Context Protocol) | `McpToolset`, `ToolboxToolset` |
| **Talk to Peer Agents** | **A2A** (Agent-to-Agent) | `RemoteA2aAgent`, `to_a2a` |
| **Buy/Sell/Checkout** | **UCP** (Universal Commerce) | `ucp_sdk` (REST based) |
| **Render UI/Dashboards** | **A2UI** / **AG-UI** | `AG-UI` components |

## 2. Model Context Protocol (MCP)
Use MCP to eliminate custom API integration code. Servers advertise tools, and Ori discovers them.
- **Official Docs**: [modelcontextprotocol.io](https://modelcontextprotocol.io)
- **Best Practice**: **Single Responsibility**. Each MCP server should handle one specific domain.
- **Security**: Run local MCP servers in restricted environments. Use explicit allowlists for file system access.
- **Prompting**: Include clear "Server Instructions" in your server’s metadata so the LLM knows *when* and *how* to use the provided tools.

## 3. Agent2Agent (A2A)
Use A2A when you need expertise from a remote agent that might be built on a different framework.
- **Official Docs**: [agent2agent.info](https://agent2agent.info)
- **Standard**: A2A v1.0 (Linux Foundation).
- **Best Practice**: **Discovery First**. Always fetch the remote agent's "Agent Card" before sending a task to verify compatibility and input/output modalities.
- **Async by Default**: Implement the `POST /tasks` endpoint to acknowledge requests immediately and process them asynchronously.

## 4. Universal Commerce Protocol (UCP)
Standardizes the shopping lifecycle (catalog, cart, checkout) across any transport.
- **Official Docs**: [ucp.dev](https://ucp.dev)
- **Best Practice**: **Merchant of Record**. Ensure the business retains control of the customer relationship and remains the Merchant of Record.
- **Implementation**: Leverage existing Google Merchant Center or Shopify feeds to populate the UCP discovery layer.

## 5. Agent-to-User Interface (A2UI)
Enables agents to render interactive, streaming dashboards and components.
- **Official Docs**: [a2aprotocol.ai](https://a2aprotocol.ai) | [ag-ui.com](https://ag-ui.com)
- **Best Practice**: **Declarative, Not Executable**. Never allow agents to send raw JavaScript. Send JSON descriptions of UI components that the client-side renderer translates into native UI.
- **State Separation**: Keep the "UI Structure" (the layout) separate from the "Application State" (the data).

## 🛡️ Security Mandate
- **No Direct `.env`**: Never hardcode credentials in a protocol tool. 
- **Use `configure_integration`**: Always use the secure capture flow for keys and tokens.
- **Privacy Guardrails**: Ensure `a2a_privacy_guardrail` is attached to all A2A/MCP toolsets.
