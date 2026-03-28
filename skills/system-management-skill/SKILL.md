---
name: system-management-skill
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

## Sandbox Hygiene Rules

The sandbox (`./data/sandbox/`) uses **symlinks** as bootstrap artifacts during `evolution_verify_sandbox` pytest runs. These symlinks point back to live project files (`pyproject.toml`, `uv.lock`, existing test files) so `uv run pytest` can resolve dependencies.

**Critical constraints:**
1. **Never stage symlinked files.** Only files written via `evolution_stage_change` are real changes. Symlinks are transient scaffolding.
2. **Never copy symlinks back to the live codebase.** Copying a symlink that points to itself causes a `SameFileError` and blocks the commit pipeline.
3. **Symlinks are cleaned up automatically** after pytest verification completes. If you encounter stale symlinks in the sandbox, remove them before committing — they are not your staged work.
4. **If pytest needs additional project files** beyond `pyproject.toml` and `uv.lock`, stage real copies via `evolution_stage_change` rather than creating manual symlinks.
