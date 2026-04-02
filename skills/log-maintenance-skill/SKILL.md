---
name: log-maintenance-skill
description: "Analyzes the runtime `data/agent.log` for system errors and deduplicates bug fixes against recent github commit history to ensure no repetitive patches are proposed for identical crashes."
---

# Log Maintenance Strategy

When asked to analyze logs or parse runtime crashes, follow this workflow to ensure efficient, non-repetitive patching.

## 1. Structured Logging (Best Practice)
- **JSON Logging**: Transition to structured JSON logs. This allows for automated parsing of metadata (token usage, model latency, TraceIDs) without regex errors.
- **Correlation IDs**: Every user request should have a unique `TraceID` included in all related log entries.

## 2. Extraction & Analysis
- **Identify StackTrace**: Extract the error message and file origin from `./data/agent.log`.
- **MAPE-K Loop**: Monitor -> Analyze -> Plan -> Execute. Use this cycle for self-healing.
- **Circuit Breaker**: If an agent fails 3 times in 5 minutes on the same tool, trigger a circuit breaker to stop execution and burn tokens.

## 3. Check Git History For Deduplication (CRITICAL)
Confirm you haven't already fixed the bug:
```bash
git log -n 50 --oneline
```
- **Stale Data?** If a matching fix exists in history, the log is just stale. STOP processing.

## 4. Sandboxed Patching
- **Triage**: Find the source code file.
- **Stage**: Use `evolution_stage_change`.
- **Verify**: Use `evolution_verify_sandbox`.
- **Commit**: Use `evolution_commit_and_push`.

## Rootless Logging Considerations
- **`journald` Driver**: In rootless Docker, integrate logs into the user-space systemd journal (`journalctl --user`).
- **Decoupled Ingestion**: Use sidecar collectors (e.g., Fluent Bit) to access logs via internal pipes if volume mounting is restricted.

Final Reminder: Never loop on the same error. Always definitively check `git log` before modification!
