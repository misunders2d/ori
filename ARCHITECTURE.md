# System Architecture: Evolution-First Design

This document outlines the core architectural principles and primitives of the system. All future development must align with these guidelines.

## Core Philosophy: Architectural Economy

We prioritize **minimalism** and **evolution-first design**. 

1.  **Leverage Existing Primitives**: Before adding new infrastructure, database tables, or complex logic, first attempt to solve the problem using existing tools:
    *   **Agent Instructions**: Many "features" can be implemented by refining the agent's system prompt.
    *   **Scheduler**: Use the existing `schedule_system_task` for background execution and periodic maintenance.
    *   **Session State**: Use ADK session state for short-term memory and flags.
    *   **Memory Skill**: Use the `remember_info` / `search_memory` tools for long-term persistence.

2.  **Reject Over-Engineering**: Avoid introducing:
    *   New background worker frameworks (we have APScheduler).
    *   New messaging queues.
    *   Complex parallel orchestration when sequential logic suffices.

3.  **Generator-Reviewer Loop**: All code changes must pass through a strict internal audit. The ReviewerAgent acts as a logic gate to prevent architectural drift and complexity bloat.

## System Primitives

### 1. Agents
*   **GeneratorAgent**: Implements code changes in the sandbox.
*   **ReviewerAgent**: Security auditor and quality gate.
*   **CommitterAgent**: Final deployment handler.
*   **CoordinatorAgent**: Orchestrates user interaction and agent transfers.

### 2. Tools
Tools are the system's "hands." They must be:
*   **Atomic**: Perform one clear action.
*   **Safe**: Validate inputs and handle errors gracefully.
*   **Documented**: Clear docstrings and type hints for the LLM to understand.

### 3. Guardrails
Guardrails are mandatory event callbacks that enforce security and safety boundaries (e.g., `admin_only_guardrail`).

### 4. Background Execution
Background tasks are handled via `app/tasks.py` and scheduled using `schedule_system_task`. They run in isolated, ephemeral sessions to prevent state contamination.
