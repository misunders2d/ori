# Changelog

All notable changes to the Ori framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/). Version bumps follow [Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-03-29

### Changed
- **Strengthened Skill Creator Protocol** — Updated the `skill-creator-skill` to mandate a "Research & Context" phase. Agents must now actively use the `external-research-skill` to fetch official documentation or repository context before drafting new skills or integrations, eliminating hallucinations in new capability development.
- **Improved Evolution Tool Robustness** — Switched `evolution_verify_sandbox` from `uv run pytest` to `sys.executable -m pytest`. This ensures test verification works reliably in local environments where nested `pyproject.toml` files might otherwise cause `uv` environment detection conflicts.

## [0.4.0] - 2026-03-29

### Added
- **Mandatory Functional Testing Protocol** — Formalized a strict requirement for all new features and bug fixes to include corresponding functional tests in the `tests/` directory.
- **Persistent Regression Safety Net** — Added `tests/test_installer.py`, `tests/test_executor.py`, and `tests/test_evolution_hygiene.py` to permanently verify core framework logic (installer detachment, tool confirmation formatting, and sandbox hygiene).
- **Updated System Management Skill** — Integrated the Testing Mandate into the `system-management-skill` to guide all future self-evolution cycles.

## [0.3.4] - 2026-03-29

### Fixed
- **UnboundLocalError in Tool Confirmation** — Fixed a bug in `app/core/agent_executor.py` where `clean_payload` could be accessed before being defined during tool confirmation message generation.

## [0.3.3] - 2026-03-29

### Fixed
- **Harden Evolution Sandbox Hygiene** — Updated `evolution_commit_and_push` in `app/tools/evolution.py` to recursively ignore all hidden dot-directories (e.g., `.pytest_cache`, `.venv`, `.git`) during staging. This prevents shell argument limit errors and accidental indexing of thousands of transient library files.
- **Improved System Management Skill** — Formalized the "Clean-Before-Commit" protocol in the `system-management-skill` to guide future self-evolution cycles in maintaining a clean sandbox state.

## [0.3.2] - 2026-03-29

### Changed
- **Enhanced Tool Confirmation Messages** — Improved the `extract_agent_response` logic in `app/core/agent_executor.py` to provide more context during tool confirmations. The messages now include the calling agent's name and a meaningful reason or argument summary (e.g., commit messages, refresh modes, or deploy descriptions) instead of generic technical hints.

## [0.3.1] - 2026-03-29

### Changed
- **Updated System Management Skill** — Expanded "Sandbox Hygiene Rules" with instructions for handling transient build artifacts (`.venv`, `.pytest_cache`, `__pycache__`) and resolving large commit errors. This ensures future self-evolution cycles handle sandbox clutter correctly.

## [0.3.0] - 2026-03-29

### Added
- **One-Liner Installation (Detached Mode)** — Added `scripts/install.sh` (Linux/macOS) and `scripts/install.ps1` (Windows) for rapid deployment. These scripts clone the repository, sever the git connection to the original repository (DNA detachment), and launch the interactive setup wizard.
- **Detached Mode Documentation** — Updated `README.md` to feature the quick install method as the primary onboarding path.
- **Harden Evolution Logic** — Modified `evolution_commit_and_push` in `app/tools/evolution.py` to automatically skip system directories (`.venv`, `.git`, `__pycache__`) and chunk git operations to avoid shell argument limits.

## [0.2.0] - 2026-03-28

### Added
- **User preferences system** — each user gets a persistent profile (`./data/preferences/{user_id}.md`) loaded into the agent's system prompt on every turn. Agents save preferences automatically when users express them.
- **Origins Protocol** — every instance knows its upstream repo (`https://github.com/misunders2d/ori`) and can check for latest improvements, security fixes, and new features at the user's discretion. Documented in `system-management-skill`.
- **Configurable bot name** — `BOT_NAME` in `.env`, settable via `/init` or by asking the agent. Defaults to "Ori". Used in all user-facing messages and agent instructions via `{bot_name}` state key.
- **`/reset` command** — transport-level session wipe that bypasses the agent entirely. Fixes the catch-22 where context limit errors left users stuck with no way to recover.
- **First-start setup banner** — console prints admin passcode, exact `/init` syntax, and `.env` location on first boot when no `GOOGLE_API_KEY` is configured.
- **Auto-generated admin passcode** — `ADMIN_PASSCODE` is created via `secrets.token_urlsafe(16)` on first start and persisted to `.env`.
- **Changelog and versioning mandate** — developer agent is now required to bump version and append to this changelog for all meaningful updates.

### Fixed
- **Interrupted messages lost from context** — when a user sends a new message mid-response, the original message is now persisted to the session via `process_message_for_context` before cancellation, matching the group-chat background processing pattern.
- **SameFileError on sandbox commit** — symlinks (`uv.lock`, `pyproject.toml`, backfilled test files) created during `evolution_verify_sandbox` pytest runs are now cleaned up after verification and skipped during `evolution_commit_and_push` walks.
- **Context limit crash unrecoverable** — error message now directs users to `/reset` instead of the borough-based "wait a few minutes".
- **`/start` gave no setup guidance** — now shows full onboarding instructions when unconfigured.
- **"Visit /setup page" referenced nonexistent page** — replaced with actual `/init` instructions.
- **Deploy watcher stuck on git edge cases** — the update loop's git topology analysis could permanently stall on diverged histories, uncommitted local changes, or corrupted HEAD, requiring a manual container restart. Replaced with unconditional `fetch` + `reset --hard origin/master` + `clean -fd`, enforcing the remote as the sole source of truth.
- **Rollback undone by next deploy cycle** — rollback scripts did a local-only `git reset` which the deploy watcher would immediately overwrite on the next trigger. Rollback now creates a `git revert` commit and pushes it to `origin/master`, keeping the remote as the authoritative source. Same fix applied to the internal deploy rollback function.

## [0.1.0] - Initial Release

### Added
- Core dual-agent architecture (CoordinatorAgent + DeveloperAgent) on Google ADK
- Self-evolution sandbox pipeline (read → stage → verify → commit)
- Telegram transport adapter with long-polling
- Secure credential capture system (transport-level interception)
- APScheduler integration for one-off and recurring tasks
- Prompt injection guardrails (semantic embedding-based)
- Admin-only access control with `ADMIN_USER_IDS`
- Skill system with progressive disclosure (`skills/` directory)
- Session compaction via `LlmEventSummarizer`
- Docker deployment with start/deploy/rollback scripts
