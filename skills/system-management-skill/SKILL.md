---
name: system-management-skill
description: "Critical execution rules for the Core Lifecycle Tools that govern the Daemon in Rootless Mode."
---

# System Management Constraints (Rootless Mode)

The `ori` daemon operates in a **Rootless Architecture**. Local source code is **Read-Only**.

## Core System Tools
1. **`update_self`**: Exit 100 -> Git pull and restart.
2. **`session_refresh`**: Wipe/summarize SQLite DB context.
3. **`trigger_rollback`**: Exit 101 -> Revert to previous commit.
4. **`set_planner_mode`**: Toggles deep-thinking inference.

## Rootless Runtime & Persistence
- **UID/GID Mapping**: Matches `agentuser` inside the container to the host UID (usually 1000) to avoid "Permission Denied" errors on writeable volumes.
- **`systemd --user`**: Use systemd user units with `loginctl enable-linger` to ensure the daemon stays active after user logout.
- **Auto-Restart**: Rely on `systemd` user units with `Restart=on-failure` for higher reliability than Docker `--restart`.

## Stability & Monitoring
- **Health Checks**: Implement a `/health` endpoint for external monitoring. Timeout should be 2–5s due to `slirp4netns` overhead.
- **Checkpointing**: Use application-level snapshots (JSON state in Redis/SQLite) for state persistence across crashes.
- **Host-Side Watchdog**: Monitors `.crash_count`. Reverts code if bot crashes 3 times consecutively.

## Security Constraints
1. **Read-Only DNA**: CANNOT write to `/code`. Evolution MUST occur via Remote.
2. **Admin-Only**: Guarded by `admin_only_guardrail`.
3. **Guardrail Integrity**: NEVER bypass `before/after` callbacks.

## Evolution via Remote
All code changes MUST be pushed to GitHub using `evolution_commit_and_push`. The `start.sh` script handles the `git pull` after an **Exit 100**.

## Regression Testing Mandate
Every change **MUST** include a test file in `tests/`. `evolution_verify_sandbox` must invoke the entire suite (`uv run pytest tests`).

## Origins Protocol
- **Upstream check**: Monitor `https://github.com/misunders2d/ori`.
- **Selective adoption**: Present upstream changes as proposals.
- **Signature Mandate**: Commits MUST be signed "evolved by {bot_name}".
