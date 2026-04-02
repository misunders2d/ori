---
name: google-adk-skill
description: "Reference material for the Google Advanced Agentic Development Kit (ADK). Use this when tasked with building new Agents, managing state memory, orchestrating sequential/parallel behaviors, or modifying Python integrations/tools in the root `app/` structure."
---

# Google ADK Workflow & Patterns (Rootless Edition)

This skill serves as the foundational knowledge base for modifying or adding ADK functionality to this repository. In this project, the framework is governed by a **Read-Only Root** constraint.

## Core Hierarchy

The Google ADK breaks down into standard primitives:
*   **`Agent`**: The core execution wrapper (LLM or Sequential routing).
*   **`Tool`**: Handlers that interact with external data or side-effects.
*   **`Session` & `State`**: Stateful variables isolated correctly per-user or globally (stored in writeable `./data/`).
*   **`Event`**: Actions pushed into the history log.

**CRITICAL**: DO NOT assume standard Python script models. When building for ADK, tools must follow specific type-hint bounds and return strictly string/dict objects, wrapped appropriately.

**ROOTLESS CONSTRAINT**: You cannot use standard Python `open(..., 'w')` on files in `app/`, `skills/`, or the root directory. You MUST use the Evolution tools to stage changes in `data/sandbox` and push them to GitHub via `evolution_commit_and_push`.

## Evolution Workflow Reference

To add or modify an ADK component in a Rootless environment:

```python
# 1. Stage the new tool in the writeable sandbox
evolution_stage_change(
    file_path="app/tools/my_new_tool.py",
    new_content="import google.adk.tools\n..."
)

# 2. Verify syntax and imports
evolution_verify_sandbox(check="syntax", target="app/tools/my_new_tool.py")
evolution_verify_sandbox(check="import", target="app.tools.my_new_tool")

# 3. Commit and push to Remote
evolution_commit_and_push(commit_message="feat: added new ADK tool")

# 4. Finalize via Coordinator
# Inform Coordinator to call update_self() to pull changes and restart.
```

## Deep Reference

If you need the exact syntax for setting up loop agents, attaching callbacks, forcing human confirmation on tools, binding structured pydantic models to `output_schema`, or injecting parameters dynamically into prompts:

**You must read the official documentation sources, examples using your github toolset or webfetch:

comprehensive ADK cheatsheet located at**:  
`references/adk-cheatsheet.md`

## External Protocols (MCP, A2A, UCP)

**You must use the `agent-protocol-skill`** when implementing:
- **MCP**: Connecting to databases, Notion, Slack, etc. via `McpToolset`.
- **A2A**: Communicating with remote peer agents.
- **UCP**: Universal Commerce and checkout flows.
- **A2UI**: Rendering rich, interactive user interfaces.

## Transport Adapter Pattern

The application supports multiple messaging platforms via the `TransportAdapter` ABC in `app/core/transport.py`. 

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
4. `update_self`: Pulls the latest code from Remote, rebuilds the Docker daemon, and restarts.

**MANDATORY RULE:** Because these tools are highly destructive or state-altering, they are protected by the `admin_only_guardrail` callback which ensures only admin users can invoke them. They are registered as plain functions on the `CoordinatorAgent`.
