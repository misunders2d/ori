# Runbook

Operational procedures: deploy, restart, rollback, recover, troubleshoot. Read this when something is broken or about to be.

> Source: `deploy/`, `app/tools/system.py`, `app/tools/evolution.py`.

---

## 1. Architecture quick-recap

- **Bot process**: `run_bot.py` — single long-running Python process bootstrapped by `deploy/start.sh`.
- **Supervisor**: `deploy/ori-supervisor.py` — restarts the bot process on clean exits (used to react to `data/.exit_signal`).
- **Persistence**:
  - `data/vault/credentials.json` — secrets (vault, atomic writes, auto-backup).
  - `data/scheduler.db` — APScheduler jobs.
  - `data/pending_actions.db` — admin ACT-token staging.
  - `data/model_config.json` — model assignments + provider cache.
  - `data/friends.json` + `data/a2a_keys.json` — A2A peers.
  - `data/.exit_signal` — supervisor handshake.
  - `data/sandbox/` — self-evolution staging area.
  - `data/evo-work/` — local-commit worktree (transient).
- **Transports**: Telegram (always), Slack (optional, via SLACK_BOT_TOKEN + SLACK_APP_TOKEN), CLI fallback.
- **External services**: Neo4j (knowledge graph), LanceDB (embeddings), Gemini/Claude/OpenRouter (LLMs).

---

## 2. Deploy

### First-time install (host)

```
git clone <repo>
cd <repo>
./deploy/install.sh
```

Installs as a user-level systemd service on Linux, `launchd` on macOS. Runs the setup wizard on first launch to capture API keys.

### Update existing deployment

Two paths:

| Trigger | What happens |
|---|---|
| `evolution_commit_and_push` succeeds | Bot writes `data/.exit_signal=EXIT_CODE_UPDATE` → supervisor pulls latest master → restarts container |
| Manual pull on host | `cd <repo> && git pull && ./deploy/start.sh` (start.sh is idempotent: restarts existing service, doesn't reinstall) |

### Worktree caveat (lesson from 2026-05-11 rescue)

The deployment may be a **worktree**, not a single checkout. `git worktree list` is the first command to run after `cd` into a project subdir — operating on the wrong worktree was the root cause of the May incident. The supervisor's pull script must:

1. Detect the worktree at the deployment path (`git rev-parse --git-common-dir` differs from `.git`).
2. Resolve which branch is checked out **there**, not in some sibling worktree.
3. Operate on that branch only.

`deploy/start.sh` and `deploy/ori-supervisor.py` already check worktree awareness — do not bypass.

---

## 3. Restart

### Clean restart

```
./deploy/start.sh    # or `systemctl --user restart ori`
```

### Rollback (revert to previous tag)

If a recent update broke things:

```
./deploy/rollback.sh    # rolls back to the previous git tag
```

Or programmatically from inside the bot (admin-only, ACT+TOTP gated):
- `trigger_rollback(reason)` — writes `EXIT_CODE_ROLLBACK`; supervisor checks out the previous tag and restarts.

---

## 4. Stop

```
./deploy/stop.sh
```

Graceful: sends SIGTERM, waits up to 30s for in-flight requests to finish, then SIGKILL.

---

## 5. Logs

```
./deploy/logs.sh             # tail systemd journal for the ori service
journalctl --user -u ori     # same, explicit
tail -f data/logs/*.log      # bot-side logging (not the systemd journal)
```

Key things to grep for:

| Symptom | grep pattern |
|---|---|
| Model errors | `error.*model\|provider` |
| Transport down | `slack\|telegram.*disconnect` |
| Evolution failed | `evolution_verify_sandbox.*fail` |
| Approval timeout | `pending_actions.*expired` |
| A2A connection issues | `a2a.*401\|a2a.*timeout` |
| Neo4j connection | `neo4j.*Connection\|driver.*unavailable` |

---

## 6. Disaster recovery

### Vault corrupt

`deploy/vault.py` keeps a backup automatically. If `credentials.json` won't load:

```
cp data/vault/credentials.json.bak data/vault/credentials.json
./deploy/start.sh
```

If both copies are corrupt, the only recovery is hand-rebuilding from the setup wizard (`uv run python interfaces/setup_wizard.py`) — secrets are not stored elsewhere by design.

### Local branch desynced (a la May 2026)

Symptom: `git pull` rejects with "divergent branches" / "non-fast-forward".

Recovery steps:

1. **DO NOT** `git reset --hard` immediately. First, save what's local:
   ```
   git tag rescue-$(date +%s)
   git branch rescue-local-$(date +%s)
   git stash list
   git reflog show HEAD | head -50          # find pre-divergence HEAD
   git fsck --lost-found --no-reflogs       # find dangling commits
   ```
2. Decide whether local changes are work that needs to be saved (push as `rescue/<topic>` to origin) or just stale (discard).
3. Only then `git reset --hard origin/<branch>` if discarding.

A reflog entry tagged `commit: ...` indicates work that was checked in locally; if it's not on origin and you reset, it's gone.

### Production process won't start

Order of operations:

1. `journalctl --user -u ori -n 200` — read the actual error.
2. Common failure: missing module after a partial update. `uv sync` reinstalls deps.
3. Common failure: vault key missing. The wizard repairs it: `uv run python interfaces/setup_wizard.py`.
4. Common failure: an evolution committed a broken file. `git revert HEAD && ./deploy/start.sh`.

### Neo4j down

The agent will still start; memory-related tools return `{"status": "error", "message": "Neo4j unavailable"}`. Restart Neo4j Aura → bot reconnects on the next memory call (driver is lazy).

Health check: `uv run python scripts/check_connectivity.py` pings every external dep and reports status.

### Scheduler stuck

`scheduler.db` (SQLite) may have an in-flight job locked by a dead process.

```
./deploy/stop.sh
uv run python scripts/reset_scheduled_tasks.py    # marks all jobs as ready
./deploy/start.sh
```

This does **not** delete jobs — they re-fire on their next schedule.

---

## 7. Self-evolution recovery

| Situation | Action |
|---|---|
| Stage failed verification | `evolution_discard_sandbox`, then re-stage. |
| Commit succeeded but bot won't start after restart | Supervisor will loop. Stop via `./deploy/stop.sh`, then on the host: `git revert HEAD && ./deploy/start.sh`. |
| Sandbox marker is stale (`>24h`) | Next `evolution_stage_change` auto-wipes (see `_sandbox_cycle_begin`). No action needed. |
| Worktree at `data/evo-work` lingering | `git worktree remove data/evo-work --force` — only if no in-flight commit. Check `git worktree list` first. |

---

## 8. Migration / backup

### Move to a new host

1. On old host: `./deploy/stop.sh`.
2. `tar -czf ori-backup.tgz data/` — captures vault, scheduler, models, friends, knowledge-graph driver config.
3. On new host: clone repo, extract tarball into project root, `./deploy/install.sh`.
4. Verify `uv run python scripts/check_connectivity.py` passes.
5. Pre-flight: send `/identity` from Telegram to confirm the bot is alive.

### Restore from snapshot

Same as above — vault + scheduler + model_config + friends are the only stateful data.

Knowledge graph (Neo4j Aura) and embeddings (LanceDB) are external — they live on their own infrastructure and are not part of the host backup.

---

## 9. Useful one-liners

```
# Show currently assigned models
uv run python -c "from app.app_utils.models import get_all_model_strings; import json; print(json.dumps(get_all_model_strings(), indent=2))"

# List active scheduled tasks
uv run python scripts/inspect_scheduled_task.py

# Show pending admin approvals
sqlite3 data/pending_actions.db 'SELECT token, tool_name, expires_at FROM actions;'

# Health-check every external dep
python scripts/check_connectivity.py
```

---

## 10. Hard rules during an incident

1. **Don't do destructive git ops in the heat of debugging.** Always tag + branch first.
2. **Worktree-aware.** `git worktree list` before any reset or pull.
3. **One change at a time.** When the bot is broken, change one variable, restart, observe — don't bundle.
4. **Read the journal before grep.** `journalctl --user -u ori -n 200` shows the real error 90% of the time.
5. **Vault is read-only during incidents.** If a credential is wrong, fix it via the setup wizard, not by hand-editing JSON.
