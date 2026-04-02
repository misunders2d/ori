---
name: system-management-skill
description: Critical execution rules for the Core Lifecycle Tools that govern the Daemon in Rootless Mode.
---

# System Management Constraints (Rootless Mode)

The `ori` daemon is a fully integrated, continuously polling worker node operating in a **Rootless Architecture**. The local source code directory (`/code`) is mounted as **Read-Only**. It manages its own persistent execution via four system-critical tools defined in `app/tools/system.py`.

## Core System Tools

1. **`update_self`**: Signals the host supervisor (Exit 100) to pull the latest codebase from GitHub and rebuild/restart the container.
2. **`session_refresh`**: Wipes or summarizes SQLite DB context blocks in the writeable `data/` directory.
3. **`trigger_rollback`**: Signals the host supervisor (Exit 101) to revert to the previous git commit and restart.
4. **`set_planner_mode`**: Dynamically toggles deep-thinking inference (`BuiltInPlanner`) execution for the current session.

## MANDATORY Security Constraints

1. **Read-Only Project DNA**: You CANNOT write to `/code`. Any attempt to modify files directly in the project root or subdirectories (except `data/`) will fail. Evolution must occur via Remote.
2. **Admin-Only Execution:** These four tools are mapped to the `CoordinatorAgent` and protected by the `admin_only_guardrail` callback. Only users in `ADMIN_USER_IDS` may invoke them.
3. **Never Remove From Root Agent:** These tools belong permanently bound to the `CoordinatorAgent`. Do NOT attempt to mount them internally into sub-agents unless explicitly architecting a new confirmation matrix.
4. **Runner Lifecycle:** DO NOT attempt to rewrite `run_bot.py`'s daemon lifecycle or memory references without explicit user permission. The async loop handles complex APScheduler and Messenger state interactions precisely.
5. **Guardrail Protection:** The guardrails (event callbacks like `before_agent_callback`, `before_model_callback`, `before_tool_callback`, and `after_tool_callback`) are critical for system safety and security. You MUST NOT remove, modify, or try to bypass these guardrails under any circumstances.

Always test any changes thoroughly in your isolated sandbox verification pipeline (`evolution_stage_change` -> `evolution_verify_sandbox`).

## GIT INTEGRITY & SAFETY

1. **Gitignore Preservation**: Never remove lines from `.gitignore`. They are essential for protecting secrets and runtime databases. 
2. **Evolution via Remote**: All code changes MUST be pushed to the remote GitHub repository using `evolution_commit_and_push`. The host-side script (`start.sh`) is the only entity that can modify the local source code by performing a `git pull` after receiving an **Exit 100** signal.

## REGRESSION TESTING MANDATE

Every feature, bug fix, or code improvement **MUST** include a corresponding functional test file in the `tests/` directory.

**Testing Protocol:**
1. **Develop Real Tests:** No code change is complete until its corresponding test file (e.g., `tests/test_feature_name.py`) is staged and verified.
2. **Persistence:** These tests must be saved permanently to the repository. 
3. **Full Suite Runs:** Every `evolution_verify_sandbox` cycle must invoke the entire existing test suite (`uv run pytest tests`). Any failure in any test must block the push.

## First-Start Setup Flow

On first start with no `ADMIN_PASSCODE` in `.env`, `run_bot.py` auto-generates a random passcode, writes it to `.env`, and prints a setup banner to the console.

**Critical constraints:**
1. **Never change the passcode generation logic** without explicit user permission.
2. **Never expose the passcode in Telegram messages.**
3. **The `/init` command deletes itself from chat** (line 373 in `telegram_poller.py`) since it may contain inline credentials. Do not remove this behavior.

## Origins Protocol

Every Ori instance is a fork of the original upstream repository: `https://github.com/misunders2d/ori`

**Capabilities:**
1. **Upstream check:** Use `web_fetch` to read the upstream repository. Compare upstream changes against the local codebase via `evolution_read_file`.
2. **Selective adoption:** Present upstream changes to the user as proposals — never auto-merge.
3. **Signature Mandate**: All git commits authored by the agent **MUST** be signed with the phrase "evolved by {bot_name}". The `evolution_commit_and_push` tool handles the git commit signature automatically.

## Docker & Container Architecture

### Bind-Mount vs Overlay Layers
The container's `/code` directory is mounted as Read-Only. Only `./data/` and `/home/agentuser/` are writeable bind-mounts. Overlay layers are copy-on-write but will not persist across container restarts.

### UID/GID Remapping
The `entrypoint.sh` remaps `agentuser`'s UID/GID to match the host. It runs `chown -R agentuser:agentgroup /home/agentuser` and fixes permissions for the writeable `data/` directory.

### Host-Side Watchdog
The host supervisor monitors `data/.crash_count`. If the bot crashes 3 times consecutively, the host will automatically perform an emergency rollback (`git reset --hard HEAD~1`) and rebuild the image.

## Sandbox Hygiene Rules

The sandbox (`./data/sandbox/`) uses **symlinks** as bootstrap artifacts during verification.

**Critical constraints:**
1. **Never stage symlinked files.** Only files written via `evolution_stage_change` are real changes.
2. **Ignore transient build artifacts:** Ensure `.venv`, `.pytest_cache`, and `__pycache__` never leak into the permanent project structure.
3. **Clean-Before-Push Protocol:** Before invoking `evolution_commit_and_push`, ensure only intended code changes are present in the sandbox.
