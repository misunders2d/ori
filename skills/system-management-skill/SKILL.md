---
name: system-management-skill
description: Critical execution rules for the Core Lifecycle Tools that govern the Daemon in Rootless Mode.
---

# System Management Constraints (Rootless Mode)

The `ori` daemon is a fully integrated, continuously polling worker node operating in a **Rootless Architecture**. The local source code directory (`/code`) is mounted as **Read-Only**.

## Core System Tools

1.  **`update_self`**: Signals the host supervisor (Exit 100) to pull the latest codebase from GitHub and rebuild/restart the container.
2.  **`session_refresh`**: Wipes or summarizes SQLite DB context blocks in the writeable `data/` directory.
3.  **`trigger_rollback`**: Signals the host supervisor (Exit 101) to revert to the previous git commit and restart.
4.  **`set_planner_mode`**: Dynamically toggles deep-thinking inference (`BuiltInPlanner`) execution for the current session.

## MANDATORY Security & Privilege Constraints

1.  **Read-Only Project DNA**: You CANNOT write to `/code`. Any attempt to modify files directly in the project root or subdirectories (except `data/`) will fail. 
2.  **Evolution via Remote**: All code changes MUST be pushed to the remote GitHub repository using `evolution_commit_and_push`. 
3.  **Host-Side Enforcement**: The host-side script (`start.sh`) is the only entity that can modify the local source code by performing a `git pull` after receiving an **Exit 100** signal.
4.  **Admin-Only Execution**: These tools are permanently bound to the `CoordinatorAgent` and protected by the `admin_only_guardrail`. Only users in `ADMIN_USER_IDS` may invoke them.
5.  **Guardrail Integrity**: You MUST NOT remove, modify, or try to bypass guardrails (event callbacks) under any circumstances. They are the constitutional limit on your autonomy.
6.  **Runner Lifecycle**: DO NOT attempt to rewrite `run_bot.py`'s daemon lifecycle. The async loop handles complex APScheduler and Messenger state interactions precisely.

## The Evolution Lifecycle (Rootless Workflow)

When the `DeveloperAgent` needs to evolve the codebase, it must follow this sequence:

1.  **Stage**: Use `evolution_stage_change` to write the new content into the writeable `data/sandbox/` directory.
2.  **Verify**: Use `evolution_verify_sandbox` to run syntax checks and the full `pytest` suite within that sandbox.
3.  **Push**: Use `evolution_commit_and_push` to commit the verified changes and push them to GitHub.
4.  **Rebirth**: Inform the `CoordinatorAgent` that changes are pushed. The Coordinator must then call `update_self` to trigger the host-side pull and rebuild.

## REGRESSION TESTING MANDATE

Every feature, bug fix, or code improvement **MUST** include a corresponding functional test file in the `tests/` directory.

1.  **Develop Real Tests**: No code change is complete until its corresponding test file is staged and verified.
2.  **Full Suite Runs**: Every `evolution_verify_sandbox` cycle must invoke the entire existing test suite (`uv run pytest tests`). Any failure must block the commit.

## First-Start Setup Flow

On first start with no `ADMIN_PASSCODE`, `run_bot.py` generates a random passcode and prints a setup banner to the console.

1.  **Passcode Preservation**: Never change the passcode generation logic.
2.  **Privacy**: Never expose the passcode in Telegram messages.
3.  **Self-Deletion**: The `/init` command deletes itself from chat to protect credentials. Do not remove this.

## Origins Protocol

Every Ori instance is a fork of: `https://github.com/misunders2d/ori`

1.  **Upstream Check**: Use `web_fetch` to check the original repo for updates or security fixes.
2.  **Selective Adoption**: Present upstream changes as proposals. Never auto-sync.
3.  **Signature Mandate**: All git commits and `CHANGELOG.md` entries authored by the agent **MUST** be signed with the phrase "evolved by {bot_name}".

## Docker & Container Architecture

### UID/GID Remapping
`entrypoint.sh` remaps `agentuser` to match host IDs. Only `./data/` and `/home/agentuser/` are writeable. Do not attempt to modify ownership or permissions inside `/code`.

### Host-Side Watchdog
The host supervisor monitors `data/.crash_count`. If the bot crashes 3 times consecutively, the host will automatically perform an emergency rollback (`git reset --hard HEAD~1`) and rebuild the image.

## Sandbox Hygiene Rules

The sandbox (`./data/sandbox/`) uses **symlinks** for bootstrap artifacts.

1.  **Never stage symlinked files**. Only files written via `evolution_stage_change` are real changes.
2.  **Ignore build artifacts**: Ensure `.venv`, `.pytest_cache`, and `__pycache__` never leak into the permanent project structure.
3.  **Clean-Before-Push**: Before invoking `evolution_commit_and_push`, ensure only intended code changes are present in the sandbox.
