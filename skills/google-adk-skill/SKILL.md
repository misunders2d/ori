---
name: google-adk-skill
description: "Reference material for the Google Advanced Agentic Development Kit (ADK). Use this when tasked with building new Agents, managing state memory, orchestrating sequential/parallel behaviors, or modifying Python integrations/tools in the root `app/` structure."
---

# Google ADK Workflow & Patterns

This skill serves as the foundational knowledge base for modifying or adding ADK functionality to this repository. You must consult this skill whenever building new agents, structuring dynamic sessions, or exposing new python methods as available sub-tools to LLMs.

## Core Hierarchy

The Google ADK breaks down into standard primitives:
*   **`Agent`**: The core execution wrapper (LLM or Sequential routing).
*   **`Tool`**: Handlers that interact with external data or side-effects.
*   **`Session` & `State`**: Stateful variables isolated correctly per-user or globally.
*   **`Event`**: Actions pushed into the history log.

**CRITICAL**: DO NOT assume standard Python script models. When building for ADK, tools must follow specific type-hint bounds and return strictly string/dict objects, wrapped appropriately.

## Deep Reference

If you need the exact syntax for setting up loop agents, attaching callbacks, forcing human confirmation on tools, binding structured pydantic models to `output_schema`, or injecting parameters dynamically into prompts:

**You must read the official documentation sources, examples using your github toolset or webfetch:

comprehensive ADK cheatsheet located at**:  
`references/adk-cheatsheet.md`

Only read the full cheatsheet if you are confused about the syntax or need exactly the right parameter string for an `Agent(...)` class invocation.

## External Protocols (MCP, A2A, UCP)

ADK has built-in support for connecting to external data and peer agents via standard protocols.

**You must use the `agent-protocol-skill`** when implementing:
- **MCP**: Connecting to databases, Notion, Slack, etc. via `McpToolset`.
- **A2A**: Communicating with remote peer agents.
- **UCP**: Universal Commerce and checkout flows.
- **A2UI**: Rendering rich, interactive user interfaces.

## Transport Adapter Pattern

The application supports multiple messaging platforms via the `TransportAdapter` ABC in `app/core/transport.py`. When adding a new platform (Discord, Slack, etc.), you implement this interface and register it — scheduled tasks, notifications, and key capture route automatically.

**You must read the full implementation guide at**:
`examples/transport_adapter.md`

For the security-critical group chat identity isolation pattern, see:
`examples/communication_channel.md`

## Headless Integration Patterns

Building integrations for platforms like Google Drive, Facebook, or GitHub requires handling authentication in a server-side, browser-less environment without exposing inbound ports.

**You must read the approved "Dark Server" strategy at**:
`references/headless-auth-patterns.md`

## System Critical Tools & Guardrails

The ADK framework natively offloads system-level mutations to standard Tool definitions rather than relying on clunky hardcoded Python intercepts.

**The 4 System-Critical Tools are:**
1. `session_refresh`: Wipes or summarizes active user conversation histories.
2. `trigger_rollback`: Reverts the git commit and reboots the active container.
3. `set_planner_mode`: Dynamically enables/disables deep thought processing.
4. `update_self`: Pulls the latest code, rebuilds the Docker daemon, and restarts.

**MANDATORY RULE:** Because these tools are highly destructive or state-altering, they are protected by the `admin_only_guardrail` callback which ensures only admin users can invoke them. They are registered as plain functions on the `CoordinatorAgent`.
