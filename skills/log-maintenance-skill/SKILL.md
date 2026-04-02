---
name: log-maintenance-skill
description: "Analyzes the runtime `data/agent.log` for system errors and deduplicates bug fixes against recent git commit history to ensure no repetitive patches are proposed for identical crashes."
---

# Log Maintenance Workflow

When analyzing logs or patching runtime crashes, follow this procedure.

## Procedure

- [ ] Step 1: Extract the error — read `data/agent.log`, find the stack trace and originating file.
- [ ] Step 2: Deduplicate against git history (CRITICAL — see gotchas).
- [ ] Step 3: If no prior fix exists, diagnose the root cause in source code.
- [ ] Step 4: Stage fix via `evolution_stage_change`.
- [ ] Step 5: Verify via `evolution_verify_sandbox` (syntax + pytest).
- [ ] Step 6: Commit via `evolution_commit_and_push`, then reboot.

Read `references/structured-logging.md` when implementing logging improvements or adding correlation IDs.

## Gotchas

- **Always check git history before patching**: Run `git log -n 50 --oneline` and search for the error signature. If a matching fix already exists, the log entry is stale from before the last reboot. STOP — do not re-patch.
- **Log prefix `Gate:`**: Whitelist/access-control rejections use this prefix. If debugging "why didn't the bot respond," grep for `Gate:` first.
- **Stale WAL files**: After a crash, `*.db-wal` and `*.db-shm` files may cause "read-only database" errors. The entrypoint cleans these on boot, but if you see the error mid-session, the DB connection pool may be poisoned — a reboot is faster than debugging.
- **`agent.log` rotates at 100KB**: Only 1 backup is kept (`agent.log.1`). If the error is gone from the main log, check the backup.
- **Don't loop on the same error**: The `verify_retry_guardrail` caps you at 3 failed verifications. Use those attempts wisely — research externally after the first failure.
