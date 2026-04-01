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
Use MCP to eliminate custom API integration code. servers advertise tools, and Ori discovers them.

- **Implementation**: Load specific instructions from `references/mcp.md`.
- **Key Class**: `google.adk.tools.mcp_tool.McpToolset`
- **Connection**: Usually via `StdioConnectionParams`.

## 3. Agent2Agent (A2A)
Use A2A when you need expertise from a remote agent that might be built on a different framework.

- **Discovery**: Agents serve cards at `/.well-known/agent-card.json`.
- **Key Class**: `google.adk.a2a.utils.agent_to_a2a.to_a2a` (to expose) or `RemoteA2aAgent` (to call).

## 4. Universal Commerce Protocol (UCP)
Standardizes the shopping lifecycle (catalog, cart, checkout) across any transport.

- **Discovery**: Supplier profiles at `/.well-known/ucp`.
- **Implementation**: See `references/ucp.md` for request/response schemas.

## 5. Agent-to-User Interface (A2UI)
Enables agents to render interactive, streaming dashboards and components.

- **AG-UI**: First-class support in ADK for building chat UIs with state sync.
- **Reference**: See `references/ui.md`.

## 🛡️ Security Mandate
- **No Direct `.env`**: Never hardcode credentials in a protocol tool. 
- **Use `configure_integration`**: Always use the secure capture flow for keys and tokens.
- **Privacy Guardrails**: Ensure `a2a_privacy_guardrail` is attached to all A2A/MCP toolsets.
