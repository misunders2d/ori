---
name: system-management-skill
description: "Critical execution rules for the Core Lifecycle Tools that govern the Daemon in Rootless Mode."
---

# System Management Constraints (Rootless Mode)

The `ori` daemon operates in a **Rootless Architecture**. Local source code is **Read-Only**.

## Core System Tools

| Tool | Exit Code | What Happens |
|------|-----------|--------------|
| `update_self` | 100 | Host force-pulls remote, cleans dangling files, rebuilds container. |
| `trigger_rollback` | 101 | Reverts last commit on host, rebuilds container. |
| `session_refresh` | — | Wipes/summarizes SQLite session context. |
| `set_planner_mode` | — | Toggles deep-thinking inference mode. |

## Evolution Reboot Procedure (Holy Grail)

This is the ONLY valid path for code changes. Every step is mandatory. No exceptions.

- [ ] Step 1: **Read** — understand code and logs before planning.
- [ ] Step 2: **Plan** — explain which files change and why. STOP for admin approval.
- [ ] Step 3: **Stage** — write all changes to sandbox via `evolution_stage_change`.
- [ ] Step 4: **Verify** — run `evolution_verify_sandbox` (syntax per file, then pytest). ALL tests must pass.
- [ ] Step 5: **Push** — call `evolution_commit_and_push`. Admin must approve via token (+2FA).
- [ ] Step 6: **Reboot** — request `update_self` from CoordinatorAgent to trigger exit 100.

After exit 100, the host runs: `git fetch origin master && git reset --hard origin/master && git clean -fd`, then rebuilds the container from scratch.

**An evolution is NOT complete until the reboot fires.** Stopping at step 5 leaves old code running — a split-brain integrity violation.

**System-critical files** (`pyproject.toml`, `config.py`, `Dockerfile`): Use `skip_local_update=True` — the auto-reboot triggers automatically.

## Sandbox Dependency Resolution (Auto-Bootstrap)

When evolving the agent's code, the `evolution_verify_sandbox` tool uses a **virtualized project structure**.

1.  **Isolation**: The sandbox only physically contains files created via `evolution_stage_change`.
2.  **Bootstrap**: To prevent `ModuleNotFoundError`, the system automatically creates symlinks to the existing project structure (`app/`, `skills/`, etc.) *around* your staged changes.
3.  **Conflict Handling**: If you stage a file (e.g., `app/tools/new_tool.py`), the system symlinks all other files in `app/tools/` individually so that the staged file takes precedence.
4.  **Requirement**: Always ensure that any existing local dependency (like `app/app_utils/models.py`) is either staged OR correctly symlinked by the tool. If verification fails with a missing module, verify the "Auto-Bootstrap" logic in `app/tools/evolution.py`.

## Security Constraints

1. **Read-Only DNA**: Cannot write to `/code`. Evolution MUST go via remote push.
2. **Admin-Only**: All system tools guarded by `admin_only_guardrail`.
3. **Push requires approval**: `evolution_commit_and_push` protected by `admin_tool_guardrail` (token + 2FA).
4. **Guardrail Integrity**: Never bypass `before/after` callbacks.

## Gotchas

- **Crash counter**: Stored in `data/.crash_count`. If it reaches 3, both the container entrypoint AND host `start.sh` trigger auto-rollback (`git reset --hard HEAD~1`). The counter resets after 30s of stable boot.
- **Exit code matters**: Only 100 (update) and 101 (rollback) trigger controlled rebuilds. Any other non-zero exit is treated as a crash with a 30s cooldown.
- **`git clean --exclude=data --exclude=.env`**: The force-pull cleans untracked files but preserves `data/` and `.env`. If you added a new top-level directory that isn't tracked, it will be deleted on reboot.
- **Sandbox cleared on new cycle**: The first `evolution_stage_change` call in a session wipes any stale sandbox from a previous rejected plan.
- **Health check timeout**: Use 2-5s timeout due to `slirp4netns` overhead in rootless Docker.
- **WAL mode**: SQLite databases are set to WAL mode on boot for concurrency. Don't switch them to DELETE journal mode.

## Regression Testing Mandate

Every change MUST pass the full test suite. `evolution_verify_sandbox(check="pytest")` runs `uv run pytest tests`.

## Origins Protocol

- **Upstream**: Monitor `https://github.com/misunders2d/ori` via `check_upstream`.
- **Selective adoption**: Present upstream changes as proposals — never auto-merge.
- **Signature**: Commits MUST include the `"evolved by {bot_name}"` trailer.
