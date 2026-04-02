---
name: google-adk-skill
description: "Reference material for the Google Advanced Agentic Development Kit (ADK). Use this when tasked with building new Agents, managing state memory, orchestrating sequential/parallel behaviors, or modifying Python integrations/tools in the root `app/` structure."
---

# Google ADK Workflow & Patterns (Rootless Edition)

This skill serves as the foundational knowledge base for modifying or adding ADK functionality. 
- **Official Docs**: [google.github.io/adk-docs/](https://google.github.io/adk-docs/)
- **Repository**: [github.com/google/adk-python](https://github.com/google/adk-python)

## Core Hierarchy

The Google ADK breaks down into standard primitives:
*   **`Agent`**: The "brain" (e.g., `LlmAgent`, `SequentialAgent`).
*   **`Tool`**: Function or API handlers.
*   **`Session` & `State`**: Persistent variables stored in `./data/`.
*   **`Runner`**: The execution engine that orchestrates user queries.

## Advanced Orchestration Patterns

### 1. Multi-Agent Systems (MAS)
- **Specialization over Monoliths**: Build a team of focused specialists (e.g., `FlightAgent`, `HotelAgent`) instead of one large agent.
- **Sequential Pipeline**: Agent A -> Agent B -> Agent C.
- **Parallel Execution**: Use `ParallelAgent` to fetch data from multiple sources simultaneously to reduce latency.

### 2. Human-in-the-Loop (HITL)
- **Tool Confirmation**: For sensitive actions (destructive writes, payments), use confirmation flows where the runner pauses and waits for admin approval.

### 3. Vibe Coding & Evaluators
- Use the built-in evaluation tools to run "Agent vs. Agent" benchmarks against a Golden Dataset.

## Evolution Workflow Reference

To add or modify an ADK component in a Rootless environment:

```python
# 1. Stage the new tool in the writeable sandbox
evolution_stage_change(file_path="app/tools/my_new_tool.py", ...)

# 2. Verify syntax and imports
evolution_verify_sandbox(check="syntax", target="app/tools/my_new_tool.py")

# 3. Commit and push to Remote
evolution_commit_and_push(commit_message="feat: added new ADK tool")
```

## Deep Reference
- **Comprehensive ADK cheatsheet**: `references/adk-cheatsheet.md`
- **MCP/A2A/UCP**: Use the `agent-protocol-skill`.
- **Transport Adapter Pattern**: See `examples/transport_adapter.md`.
- **Dark Server Strategy**: See `references/headless-auth-patterns.md`.

## System Critical Tools & Guardrails
The ADK framework natively offloads system-level mutations to:
1. `session_refresh`: Wipes/summarizes history.
2. `trigger_rollback`: Git revert and reboot.
3. `set_planner_mode`: Toggles deep thought.
4. `update_self`: Git pull and restart.

**MANDATORY RULE:** Protected by the `admin_only_guardrail`. Only admin users can invoke them.
