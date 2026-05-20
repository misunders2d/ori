# Runbook

Operational procedures: deploy, restart, rollback, recover, troubleshoot. Read this when something is broken or about to be.

> Source: `deploy/start.sh`, `deploy/ori-supervisor.py`, `deploy/vault.py`, `app/tools/system.py`, `app/tools/evolution.py`.

---

## 1. Architecture quick-recap

- **Bot process**: `run_bot.py` — single long-running Python process bootstrapped by `deploy/start.sh`.
- **Supervisor**: `deploy/ori-supervisor.py` — runs `run_bot.py` as a subprocess, reacts to `data/.exit_signal`, handles auto-sync, crash protection, child image rebuilds, Cloudflare tunnel refresh. See §2 below for the full contract.
- **Persistence**:
  - `data/vault/credentials.json` — secrets (vault, atomic writes, auto-backup).
  - `data/scheduler.db` — APScheduler jobs.
  - `data/pending_actions.db` — admin ACT-token staging.
  - `data/model_config.json` — model assignments + provider cache.
  - `data/friends.json` + `data/a2a_keys.json` — A2A peers.
  - `data/.exit_signal` — supervisor handshake.
  - `data/.deps_hash` — fingerprint of `pyproject.toml` + `uv.lock` for auto-sync.
  - `data/sandbox/` — self-evolution staging area.
  - `data/evo-work/` — local-commit worktree (transient).
- **Transports**: Telegram (always), Slack (optional, via SLACK_BOT_TOKEN + SLACK_APP_TOKEN), CLI fallback.
- **External services**: Neo4j (knowledge graph), LanceDB (embeddings), Gemini/Claude/OpenRouter (LLMs).

---

## 2. Supervisor contract — what `ori-supervisor.py` does for you

You almost never invoke `uv sync`, `git pull`, or `docker build` manually. The supervisor does them automatically at the right moments.

### Boot sequence (on `./deploy/start.sh` or systemd start)

1. `load_vault()` decrypts `data/vault/credentials.json` into `os.environ`.
2. `ensure_secrets()` auto-generates `ADMIN_PASSCODE` and `A2A_API_KEY` if not in vault.
3. `deps_changed()` hashes `pyproject.toml` + `uv.lock`, compares to `data/.deps_hash`. If different → `uv sync`, then writes the new hash. **This is why you don't run `uv sync` by hand — the supervisor catches lockfile changes automatically.**
4. `refresh_tunnel()` (re)starts the Cloudflare tunnel container if `deploy/docker-compose.yml` is present.
5. Starts `.venv/bin/python run_bot.py` as a child process.
6. Watches `data/.exit_signal` and the child's exit code in a loop.

### Exit-code handling

| Exit code | Meaning | Supervisor action |
|---|---|---|
| `0` | Clean shutdown (user/admin) | Stop, do not restart. |
| `100` | Evolution committed | `apply_evolution()` → `git pull` (if remote configured), reset working tree to FETCH_HEAD, run `uv sync` if deps changed, rebuild child Docker image if children running, refresh tunnel, restart bot. |
| `101` | Rollback requested | `apply_rollback()` → `git revert HEAD --no-edit`, run `uv sync` if deps changed, restart bot. |
| crash (non-zero, no signal file) | Bot died | Restart up to `MAX_CRASHES` (3) times with `COOLDOWN` (30 s) between attempts. Process stable for `STABLE_THRESHOLD` (60 s) resets the counter. After max crashes → stop and alert (no auto-rollback). |

### Implications for ops

- **Update flow on host**: `cd <repo> && git pull && ./deploy/start.sh` is enough. The supervisor decides whether `uv sync` is needed by comparing hashes. Don't pre-empt it.
- **Evolution flow from inside the bot**: agent runs `evolution_commit_and_push` → `_write_exit_signal(100)` → supervisor catches it, applies, restarts. The user does nothing.
- **Rollback flow**: agent (or admin) runs `trigger_rollback` → exit 101 → supervisor reverts one commit + restarts.
- **Crash loop**: if the bot crashes 3 times within `STABLE_THRESHOLD`, the supervisor stops. Don't fight the supervisor; read the journal (`journalctl --user -u ori -n 200`) and fix root cause.

---

## 3. Deploy

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

### Amazon Ads auth refresh

Amazon Ads access tokens are short-lived and must be minted automatically from
the vault-backed `ADS_API_REFRESH_TOKEN`. The bot should ask the user to run the
OAuth helper only when the refresh token is missing or LWA definitively rejects
it (`invalid_grant`, `invalid_client`, `unauthorized_client`, HTTP 401, or HTTP
403). Network errors, HTTP 429, and HTTP 5xx are transient; surface them loudly
and let the background refresher retry without prompting re-authorization.

Credential reads go through `deploy.vault.get()`. It must prefer `os.environ`
after normal boot hydration, then fall back to the locked vault file and hydrate
the environment for later fast-path reads. This prevents a valid refresh token
written to `data/vault/credentials.json` after process start from being treated
as absent. Never log raw LWA response bodies, access tokens, refresh tokens, or
client secrets; status codes and OAuth error codes are enough for diagnosis.

The Ads config keys belong in `ALLOWED_CONFIG_KEYS` so `/init` can provision
them, but not in `AGENT_CONFIG_KEYS`; they are integration secrets/settings, not
LLM-facing agent behavior.

---

## 4. Restart

### Clean restart from inside the chat (preferred — no shell access needed)

Admin says `reboot`, `restart`, `restart the bot`, or `call update_self` in Telegram/Slack. Coordinator calls `update_self` → writes `data/.exit_signal=100` → supervisor reads it after the response is delivered, then re-launches the bot. The reboot is **admin-only but NOT ACT/TOTP gated** (process restart is non-destructive — no code or secret writes — supervisor brings it back). Use this immediately after a `/models set` provider swap so the new model takes effect.

Pain history: until 2026-05-12, `update_self` was on the ACT-staging list, forcing a 3-turn dance (reboot → stage → "Approve ACT-xxx 123456" → finally restart). Removed because the protection didn't match the blast radius. ACT remains on `trigger_rollback`, `evolution_*`, and integration writes.

### Clean restart from the host

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

## 5. Stop

```
./deploy/stop.sh
```

Graceful: sends SIGTERM, waits up to 30s for in-flight requests to finish, then SIGKILL.

---

## 6. Logs

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

### From inside the chat — diagnostic tools (admin)

Mounted on the CoordinatorAgent via `SystemToolset`. Use these when you don't have shell access (mobile, away from desk):

| Tool | What it returns | Gating |
|---|---|---|
| `report_health` | Full system health: API connectivity, disk usage, git integrity. Calls `app/core/health.py:get_system_health`. | admin-only check |
| `check_active_tasks` | List of background `ACTIVE_TASKS` (task_id, type, status, start_time, prompt). Useful for "what's running right now?" | admin-only check |
| `inspect_secure_env` | Every env var with sensitive values redacted (`SECRET`/`TOKEN`/`KEY`/`PASSCODE` substrings → `xxx...xxx`). Useful for confirming a `.env` / vault key landed. | admin-only, no ACT (in `_ADMIN_ONLY_NO_STAGING` — env keys reveal system shape even when values are redacted) |

These three were re-wired on 2026-05-12 — previously sat as orphan code in `app/tools/diagnostics.py` (never reachable from chat). Now callable directly.

---

## 7. Disaster recovery

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
2. Common failure: missing module after a partial update. **Do not** run `uv sync` by hand — touch `data/.deps_hash` (`echo > data/.deps_hash`) and run `./deploy/start.sh`; supervisor will detect the mismatch and re-sync.
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

## 8. Self-evolution recovery

| Situation | Action |
|---|---|
| Stage failed verification | `evolution_discard_sandbox`, then re-stage. |
| Commit succeeded but bot won't start after restart | Supervisor will loop. Stop via `./deploy/stop.sh`, then on the host: `git revert HEAD && ./deploy/start.sh`. |
| Sandbox marker is stale (`>24h`) | Next `evolution_stage_change` auto-wipes (see `_sandbox_cycle_begin`). No action needed. |
| Worktree at `data/evo-work` lingering | `git worktree remove data/evo-work --force` — only if no in-flight commit. Check `git worktree list` first. |

---

## 9. Migration / backup

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

## 10. Useful one-liners

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

## 11. Hard rules during an incident

1. **Don't do destructive git ops in the heat of debugging.** Always tag + branch first.
2. **Worktree-aware.** `git worktree list` before any reset or pull.
3. **One change at a time.** When the bot is broken, change one variable, restart, observe — don't bundle.
4. **Read the journal before grep.** `journalctl --user -u ori -n 200` shows the real error 90% of the time.
5. **Vault is read-only during incidents.** If a credential is wrong, fix it via the setup wizard, not by hand-editing JSON.

---

## 12. Scheduled-task fabrication defenses (2026-05-20)

Triggered by the cron_97f22322 incident: a Mon/Wed/Fri 12:30 Kyiv
scheduled task ("FBA Shipment Discrepancy Report for Top 50 ASINs")
fired in 14.875 s with synthesized discrepancy numbers and a Slack
post saying *"Drive upload was bypassed as the account is not
connected"* — phrasing absent from every source file. OAuth refresh
succeeded mid-fire; the model simply skipped BigQuery + Drive +
Sheets entirely and invented an excuse. The bot's monitor reply
later compounded the issue with *"permission/scope mismatch at
exact moment of execution"* — also LLM-composed.

Two complementary Law-6 layers were added in `app/tasks.py:run_scheduled_task`:

**Fix 2.2 — prompt-trigger detector** (`_match_triggers` +
`_bigquery_predicate` + `PROMPT_TRIGGERS` + helper
`_check_fabrication_for_scheduled_fire`). Maps prompt-shape
predicates to required tool sets:

- `bigquery` — literal "BigQuery" OR dotted-table backtick
  (`reports.<t>`, `sellercloud.<t>`, `mellanni-medic.<ds>.<t>`) OR
  `query`/`sql` AND a known BQ companion token (conjunctive — bare
  `query`/`sql` does NOT trigger).
- `sheets/spreadsheet` → `sheets_read`/`sheets_write`/`sheets_create`/`sheets_list_tabs`.
- `csv report` → `data_to_csv`/`export_report_to_csv`/`generate_file`.
- `sp-api` → SP-API tool group.
- `upload to Drive` → **unrunnable** (no satisfying tool exists in
  this build; read-only `drive_list_files`/`drive_download_file`
  do NOT count).

Fires only on the agent-success path. Group-disjoint match → flips
`agent_ok = False`, status `"Failed (fabrication detected: ...)"`,
emits `_log_job_event("error", ...)`, admin-alerts via
`_notify_admins_safe`. Required-group miss = `logger.error`;
un-runnable = `logger.critical`. Cursor refuses to advance (existing
gate at `app/tasks.py:526-527`); recurring jobs retry next wake.

**Fix 2.3 — suspect-phrase output scan**
(`_check_suspect_phrase_for_scheduled_fire`). Curated 5-phrase tuple
`_SUSPECT_PHRASES` (drive upload was bypassed, upload was bypassed,
as the account is not connected, permission/scope mismatch, at exact
moment of execution). Runs only when Fix 2.2 passed. Three branches,
all fail-the-task:

- **Scan failure** → `logger.critical` + admin alert, status
  `"Failed (suspect-phrase scan failed)"`.
- **Real tool error paraphrased** → `logger.error` + admin alert,
  status `"Failed (suspect phrase + real tool error paraphrased)"`,
  verbatim tool errors prepended above the LLM response.
- **Pure fabrication** (suspect phrase + zero recorded tool errors)
  → `logger.critical` + admin alert, status `"Failed (suspect
  phrase + no recorded tool error: fabricated)"`.

**Fix 2.4 — monitor honesty rule** on CoordinatorAgent's
`instruction=`. When the user asks "did my task fail?",
Coordinator MUST quote `get_scheduled_task_logs` fields verbatim and
MUST NOT infer a cause beyond what those fields literally state.
Inventing a technical-sounding cause is an explicit Law-6 violation
spelled out in the prompt.

**Existing Law-6 surface preserved.** `surface_error_loudly_after_tool`
(`app/callbacks/guardrails/bouncer.py:187-232`) still wraps any tool
returning `{"status": "error", ...}` with `[TOOL FAILURE — RELAY
VERBATIM ...]` + an `agent_directive` field. It structurally cannot
fire when the model skips the tool entirely; Fix 2.2 + 2.3 cover
that gap. Pinned by `tests/test_amazon_workspace_after_tool_wrapper.py`.

**Operator playbook for a flagged cron**:

If a scheduled task gets the `"Failed (fabrication detected: ...)"`
or `"Failed (suspect phrase + ...)"` status, the LLM was not doing
the work the prompt asked for. Two real remediation paths (NOT a
detection-only patch):

1. **Edit the prompt + add enforced steps** (interim, soft
   enforcement only — `seed_plan` without `step_constraints` is
   soft per `app/tools/planner.py:76-80`):
   ```
   edit_scheduled_task(
       job_id="cron_<id>",
       new_task_prompt="<rewritten prompt referencing only registered tools>",
       new_steps=[
           "Run the BigQuery SQL", "Generate the CSV via data_to_csv",
           "Post the Slack summary",
       ],
   )
   ```
   `edit_scheduled_task` is owner/admin-gated via `_can_access_job`
   (no ACT-token/TOTP — owner drives the edit themselves).

2. **Migrate to a v1 contract** (production-grade — deterministic
   emit dispatch with no LLM improvisation). Real sequence:
   ```
   contract_from_existing(job_id="cron_<id>")
       → returns {"status": "draft", "spec": {...}}
   # Operator/agent tightens spec.inputs (bigquery_query loader
   # with the discrepancy filter inline as SQL WHERE/CASE),
   # spec.emit (sheet_append OR slack_post), spec.trigger.
   contract_dry_run(spec=tightened)
   contract_freeze(spec=tightened)  # refuses unless STRICT
       → {"status": "frozen", "id": "...", "version": N, "hash": "..."}
   contract_schedule(contract_id=<frozen.id>)
   # Operator manually deletes the legacy cron_<id> after the new
   # contract has run at least one successful fire.
   ```
   `ReasoningStep` is *"One LLM call in the reasoning chain"* per
   `app/contracts/schema.py:166-168` — NOT pure-Python. Put
   deterministic filters in the BigQuery SQL inside the loader,
   not in a `reasoning` step.

**Ghost-tool note**. `drive_upload_file` does NOT exist in
`GoogleWorkspaceToolset`. References to it in
`app/tools/presentations.py:18-19`, `docs/PRESENTATIONS.md:52`,
`skills/presentation-skill/SKILL.md:91` are slated for cleanup in a
follow-on slice. The un-runnable trigger in Fix 2.2 catches any
prompt that still asks for a Drive upload.
