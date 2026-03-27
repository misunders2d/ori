---
name: log-maintenance-skill
description: Analyzes the runtime `data/agent.log` for system errors and deduplicates bug fixes against recent github commit history to ensure no repetitive patches are proposed for identical crashes.
---

# Log Maintenance Strategy

When you are asked to analyze logs, check for bugs automatically, or parse runtime crashes sequentially, you MUST follow this precise workflow to ensure you do not repeatedly attempt to fix code you have already patched.

## 1. Extract The StackTrace
Use your `evolution_read_file` tool to read the contents of `./data/agent.log`. 
- Identify any `ERROR` or `WARNING` stacktraces or explicit crashes.
- Extract the core error message and the file it originated from. *If there are no errors, declare the system healthy and STOP.*

## 2. Check Git History For Deduplication (CRITICAL)
Before you even attempt to analyze the crash and search for the bug in code, you MUST confirm you haven't already fixed it. 
Run a terminal search using the `run_command` tool (if available) or rely on `evolution_read_file`. Usually, you can query:
```bash
git log -n 50 --oneline
```
And look for the module or error type you just found. 
- **Was a patch recently pushed resolving this?** If the git history indicates `Fix: resolved ValueError in <your module>` or similar matching your crash, *THE LOG IS JUST STALE DATA FROM BEFORE THE FIX*. 
- **Action:** Stop processing. Inform the user that the crash discovered in the log was already fixed in a recent execution block. Move on.

## 3. Sandboxed Patching
Only if the error has not been recently committed in the git log history, you may proceed:
- **Triage:** Find the source code file causing the crash.
- **Stage:** Use `evolution_stage_change` to deploy a patch inside the sandbox.
- **Verify:** Use `evolution_verify_sandbox` comprehensively. You must ensure your fix compiles natively.
- **Commit:** Use `evolution_commit_and_push` out of the sandbox to formalize the fix.

## Final Reminder
Never loop on the same error. Always definitively check `git log` before attempting to modify code from an `agent.log` trace!
