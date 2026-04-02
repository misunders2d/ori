---
name: google-adk-skill
description: "Reference material for the Google Advanced Agentic Development Kit (ADK). Use this when tasked with building new Agents, managing state memory, orchestrating sequential/parallel behaviors, or modifying Python integrations/tools in the root `app/` structure."
---

# Google ADK Workflow & Patterns (Rootless Edition)

Foundational knowledge for modifying or adding ADK functionality.
- **Official Docs**: [google.github.io/adk-docs/](https://google.github.io/adk-docs/)
- **Repository**: [github.com/google/adk-python](https://github.com/google/adk-python)

## Core Hierarchy

- **`Agent`**: The brain (`LlmAgent` for LLM-driven, `BaseAgent` for custom workflow).
- **`Tool`**: Function handlers. Defined in `app/tools/`, registered on agents.
- **`Session` & `State`**: Persistent key-value store in `./data/`. State is per-session.
- **`Runner`**: Execution engine that orchestrates queries through agents.

## When to Load References

- **Building a new agent, tool, or callback**: Read `references/adk-cheatsheet.md` — covers setup, tool definitions, state management, callbacks, workflow agents, and CLI commands.
- **Adding OAuth2 integration on a headless server**: Read `references/headless-auth-patterns.md` — covers OOB flow, Device Code flow, and ephemeral tunnels.
- **Adding protocol integrations (MCP/A2A/UCP)**: Use the `agent-protocol-skill` instead.

## Orchestration Patterns

- **Specialization over monoliths**: Build focused sub-agents (`DeveloperAgent`, `KnowledgeAgent`), not one mega-agent.
- **Sequential pipeline**: `SequentialAgent` for deterministic A -> B -> C flows.
- **Parallel execution**: `ParallelAgent` to fetch from multiple sources simultaneously.
- **Human-in-the-loop**: Use `admin_tool_guardrail` for confirmation on destructive/sensitive actions.

## Evolution Workflow for ADK Components

```python
# 1. Stage the new tool in sandbox
evolution_stage_change(file_path="app/tools/my_tool.py", ...)

# 2. Verify syntax and imports
evolution_verify_sandbox(check="syntax", target="app/tools/my_tool.py")

# 3. Run full test suite
evolution_verify_sandbox(check="pytest")

# 4. Commit, push, and reboot
evolution_commit_and_push(commit_message="feat: added new ADK tool")
# Then request update_self from CoordinatorAgent
```

## Gotchas

- **`SequentialAgent` vs `Agent` for pipelines**: Use `SequentialAgent` only for deterministic step-by-step flows. For anything requiring LLM routing decisions, use a flat `Agent` with tools. We tried a loop-based `SequentialAgent` for the developer agent — it caused infinite retries.
- **`before_tool_callback` return value**: Return `None` to allow the tool call. Return a `dict` to abort execution and send that dict as the tool response. This is how `admin_tool_guardrail` blocks privileged calls.
- **`after_tool_callback` can't modify state directly**: Use `tool_context.state[key] = value` inside the callback, not `tool_response`.
- **`SkillToolset` is experimental**: Loaded via `load_skill_from_dir()`. Skills are incremental (L1/L2/L3) — only SKILL.md frontmatter loads at discovery time.
- **State keys must be strings**: Non-string keys in `tool_context.state` will silently fail or cause serialization errors.
- **`DatabaseSessionService` uses async SQLite**: The session DB at `data/ori-sessions.db` uses `aiosqlite`. Don't open sync connections to it from tools.
- **Protected system tools**: `update_self`, `trigger_rollback`, `session_refresh`, `evolution_commit_and_push` are all gated by `admin_tool_guardrail`. Only admin users can invoke them.
