# Changelog

## [unreleased] — 2026-05-20

### Added — scheduled-task fabrication defenses (cron_97f22322 incident)

Mon/Wed/Fri 12:30-Kyiv cron `cron_97f22322` ("FBA Shipment
Discrepancy Report for Top 50 ASINs") fired in 14.875 s with
synthesized discrepancy numbers + a Slack post saying *"Note:
Google Drive upload was bypassed as the account is not
connected"* — phrasing absent from every source file in `app/`,
`skills/`, `docs/`, `interfaces/`. OAuth refresh succeeded
mid-fire; the model simply skipped BigQuery + Drive + Sheets
entirely and invented an excuse. The bot's later monitor reply
compounded the issue with *"permission/scope mismatch at exact
moment of execution"* — also LLM-composed.

Two complementary Law-6 layers added in
`app/tasks.py:run_scheduled_task` (Fix 2.2 prompt-trigger
detection + Fix 2.3 suspect-phrase output scan), one monitor-
honesty rule on CoordinatorAgent's `instruction=` (Fix 2.4),
three ghost-tool references stripped from the presentation
surface (`app/tools/presentations.py`, `docs/PRESENTATIONS.md`,
`skills/presentation-skill/SKILL.md`), one boot-time validator
(`app/core/instruction_validator.py`) running fire-and-forget
after `scheduler.start(paused=True)` to flag remaining ghost-
tool refs + at-risk persisted cron jobs.

Detection composes with the existing cursor-advance gate in
`app/tasks.py:run_scheduled_task` (`if delivered_ok and
agent_ok:`) — a flagged fire flips `agent_ok=False`, the cursor
refuses to advance, and recurring jobs re-attempt next wake via
the existing `elif delivered_ok and not agent_ok:` branch.
Regression-test files cover the helpers
(`tests/test_list_invocation_tool_calls.py`,
`tests/test_fabrication_triggers.py`,
`tests/test_fabrication_detection.py`,
`tests/test_suspect_phrase_detection.py`), the existing Law-6
surface pin (`tests/test_amazon_workspace_after_tool_wrapper.py`),
the ghost-ref strip (`tests/test_no_ghost_drive_upload_refs.py`),
and the boot validator (`tests/test_instruction_validator.py`)
— 130+ tests total.

Track 1B only — no Drive-upload tool added in this work. Either
the operator restructures the failing cron via
`edit_scheduled_task(new_steps=[...])` for interim soft
enforcement OR migrates to a v1 contract via
`contract_from_existing` → tighten spec (`bigquery_query` loader
SQL contains the discrepancy filter) → `contract_dry_run` →
`contract_freeze(spec=dict)` → `contract_schedule(contract_id)`.
Operator playbook lives in `docs/RUNBOOK.md §12`.

## [1.0.1] - 2024-03-20

### Added
- **Hardened Permission Alignment**: Added intelligent UID/GID mapping in `entrypoint.sh` to ensure the internal agent user always matches the host user, even on detached/missing `.git` repos.
- **Stale Database Lock Cleanup**: Added automatic cleanup of SQLite journal, WAL, and SHM files in `entrypoint.sh` before boot to prevent "Read-only database" errors after unclean shutdowns.
- **SELinux Support**: Added `:z` labels to all Docker bind-mounts in `docker-compose.yml` for cross-distribution compatibility.
- **Self-Evolution Infrastructure**: Included system files (`Dockerfile`, `docker-compose.yml`, etc.) in the build context and image, enabling the agent to autonomously evolve its own infrastructure.

### Fixed
- Fixed potential lockout when the agent user UID (1000) conflicted with the host user UID.
- Fixed 'git pull' permission errors during updates by resetting host-side `.git` ownership in `start.sh`.

## [1.0.0]

### Added
- Integrated `repair_data_permissions` tool into the central toolset in `app/tools/__init__.py`.
- Added functional test `tests/test_repair_data_permissions.py` to verify the filesystem repair logic.

### Fixed
- Fixed potential 'readonly database' errors by ensuring the `data/` directory and its contents have correct write permissions.

evolved by Ori
