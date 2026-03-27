---
name: System Management Constraint
description: Critical execution rules for the Core Lifecycle Tools that govern the Daemon.
---

# System Management Constraints

The `ori` daemon is a fully integrated, continuously polling worker node. It manages its own persistent execution via four system-critical tools defined in `app/tools/system.py`.

## Core System Tools

1. **`update_self`**: Pulls latest codebase and restarts the node container.
2. **`session_refresh`**: Wipes or summarizes SQLite DB context blocks.
3. **`trigger_rollback`**: Recovers a previously stable git footprint and restarts.
4. **`set_planner_mode`**: Dynamically toggles deep-thinking inference (`BuiltInPlanner`) execution for the current session.

## MANDATORY Security Constraints

1. **Never Bypass Confirmation:** These four tools are fundamentally mapped to the `CoordinatorAgent` as ADK `FunctionTool` wrappers with `require_confirmation=True`. Under NO CIRCUMSTANCES should you alter, remove, or try to bypass the `require_confirmation` wrapper. The system depends on delegating this security authorization step to the human user sitting at the chat interface. DO NOT attempt to execute them implicitly.
2. **Never Remove From Root Agent:** These tools belong permanently bound to the `CoordinatorAgent`. Do NOT attempt to mount them internally into sub-agents unless explicitly architecting a new confirmation matrix.
3. **Runner Lifecycle:** DO NOT attempt to rewrite `run_bot.py`'s daemon lifecycle or memory references without explicit user permission. The async loop handles complex APScheduler and Messenger state interactions precisely, and modifying the polling architecture without extreme caution will break the deployment container loop.

Always test any changes that brush up against `system.py` logic thoroughly in your isolated sandbox verification pipeline (`evolution_stage_change` -> `evolution_verify_sandbox`).
