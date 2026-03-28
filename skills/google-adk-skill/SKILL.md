---
name: google-adk-skill
description: "Reference material for the Google Advanced Agentic Development Kit (ADK). Use this when tasked with building new Agents, configuring LLM tool wrappers (like require_confirmation=True), managing state memory, orchestrating sequential/parallel behaviors, or modifying Python integrations/tools in the root `app/` structure."
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

**You must read the comprehensive ADK cheatsheet located at**:  
`references/adk-cheatsheet.md`

Only read the full cheatsheet if you are confused about the syntax or need exactly the right parameter string for an `Agent(...)` class invocation.

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

**MANDATORY RULE:** Because these tools are highly destructive or state-altering, you **MUST NEVER** attempt to build manual `confirmed=False` parsing inside the tool logic itself. 
Instead, whenever you register these system-critical tools to an `Agent` (like the `CoordinatorAgent`), you **MUST** wrap them using the internal framework safeguard:
```python
import google.adk.tools

...
tools=[
    google.adk.tools.FunctionTool(update_self, require_confirmation=True),
    google.adk.tools.FunctionTool(session_refresh, require_confirmation=True),
    ...
]
```
Using `require_confirmation=True` guarantees that the ADK execution graph naturally halts and invokes Human-in-the-Loop interaction before the tool is ever allowed to run, completely neutralizing accidental framework wipes without requiring any custom manual prompt parsing.

**HEADLESS CONFIRMATION ARCHITECTURE:**
Because the daemon interacts with users over headless environments (e.g. Telegram) rather than a dynamic web UI, it cannot rely on the user clicking a physical "Approve" button widget that the ADK `Runner` normally expects. 
Instead, `app/core/agent_executor.py` natively intercepts user plaintext responses (`"yes"` or `"no"`), retroactively matches them to ANY pending `requested_tool_confirmations` in the `session.events`, and dynamically binds them securely to an internal `adk_request_confirmation` ADK `FunctionResponse`.

If an LLM agent incorrectly hallucinates and tells a user to "click the Approve button in the chat prompt", it is because the foundational ADK system prompt internally injected that wording. The LLM does not natively know about the headless interceptor. If necessary, you may inject system instructions overriding this behavior, but the underlying execution framework will handle textual "yes/no" inputs natively for ALL tools with `require_confirmation=True`.
