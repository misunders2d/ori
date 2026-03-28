# Changelog

All notable changes to the Ori framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/). Version bumps follow [Semantic Versioning](https://semver.org/).

## [0.5.5] - 2026-03-31

### Changed
- **Documentation Refinement** — Updated `README.md` to prioritize "Evolutionary Engineering" over "Vibe Coding." Added comprehensive instructions for the Zero-Configuration CLI Onboarding Chat (v0.4.7), ensuring a smoother first-contact experience for new users.

evolved by Ori

## [0.5.4] - 2026-03-31

### Changed
- **Signature Boundary Enforcement** — Clarified the "Signature Mandate" in the `system-management-skill`. The "evolved by {bot_name}" signature is now strictly limited to git commits and `CHANGELOG.md` entries. It is explicitly prohibited from appearing in regular chat messages to maintain a clean user experience.

evolved by Ori

## [0.5.3] - 2026-03-31

### Changed
- **Configurable App Identity** — The internal ADK `App.name` is no longer hardcoded as "ori". It now dynamically pulls from the `APP_NAME` environment variable, allowing for easier multi-instance deployment.
- **Refined Event Compaction** — Relaxed the memory management settings to be less aggressive. `compaction_interval` is increased to 10 events (from 5) and `overlap_size` to 3 events, ensuring more fluid and coherent long-term conversations for professional assistant use cases.

evolved by Ori

## [0.5.2] - 2026-03-31

### Added
- **Intelligent Upstream Merging (Origins Protocol)** — Implemented automated upstream tracking via `app/core/origins.py`. Ori can now natively fetch the latest improvements from the master repository and identify specific code differences.
- **Origins Report Tool** — Added a `check_upstream` tool to the `CoordinatorAgent`. This generates a summary report of new commits and files available in the upstream Ori project.
- **Selective Adoption Support** — Added `analyze_upstream_file` to both Coordinator and Developer agents, allowing users to inspect code diffs before approving a merge.

evolved by Ori

## [0.5.1] - 2026-03-31

### Added
- **Proactive Self-Diagnostics (The "Nervous System")** — Implemented a background monitoring task (`app/core/health.py`) that checks Google API connectivity, poller liveness, disk usage, and git integrity every 10 minutes.
- **System Health Tool** — Added a `report_health` tool to the `CoordinatorAgent`, allowing users to manually request a detailed status report of the bot's vitals.
- **Admin Proactive Alerts** — The bot now proactively notifies admins via Telegram if any "vital signs" (e.g., API connection or poller heartbeat) are degraded.

evolved by Ori

## [0.5.0] - 2026-03-31

### Added
- **The Autonomous Auth Engine** — Implemented a core `OAuthService` (`app/core/auth.py`) and a set of `CoordinatorAgent` tools for managing platform integrations.
- **Headless OAuth2 (Device Code Flow)** — The system now natively supports the OAuth2 Device Code Flow, allowing Ori to connect to services like Google Drive, Meet, and GitHub on browser-less remote servers without opening inbound ports.
- **Background Handshake Polling** — Introduced background tasks for auth handshake polling, which proactively notify the user via their messaging platform (e.g., Telegram) once a connection is successful.

evolved by Ori

## [0.4.7] - 2026-03-31

### Added
- **Interactive CLI Onboarding Chat** — Implemented a terminal-based chat interface (`interfaces/cli_chat.py`) that automatically triggers on first boot if no messaging platforms (like Telegram) are configured. This allows new users to interact with the bot immediately after installation.
- **Automated Onboarding Prompt** — The CLI chat proactively asks for language preferences, outlines core principles (Self-evolution, safety, persistence), and guides the user through the necessary setup steps (e.g., configuring Telegram).

evolved by Ori

## [0.4.6] - 2026-03-31

### Added
- **Headless Auth Reference Patterns** — Added a comprehensive reference guide for building integrations on browser-less, inbound-blocked servers. This includes the Out-of-Band (OOB) Copy-Paste flow, Device Code flow, and Ephemeral Tunnel patterns, providing a clear roadmap for future external integrations like Google Drive or Meet.

evolved by Ori

## [0.4.5] - 2026-03-31

### Fixed
- **Pydantic Event Validation** — Fixed a `ValidationError` in `app/core/agent_executor.py` where `Event` objects created in `process_message_for_context` were missing the required `author` and `id` fields. This was causing background context-saving tasks to crash silently.
- **Skill Loading Robustness** — Reinforced the requirement for YAML frontmatter in `SKILL.md` files, which was causing boot-time crashes when incorrectly formatted.

evolved by Ori

## [0.4.4] - 2026-03-31

### Fixed
- **Sequential Context Persistence** — Implemented a `weakref.WeakValueDictionary` of session locks in `interfaces/telegram_poller.py`. This ensures that rapid-fire or forwarded messages are processed in strict sequence, preventing a race condition where previous messages were lost from history during task cancellation.
- **Enhanced Video Handling** — Added support for `video` and `video_note` file types in the Telegram poller, allowing the agent to receive and process video attachments.

evolved by Ori

## [0.4.3] - 2026-03-30

### Added
- **Automated Signature Protocol** — Updated the `evolution_commit_and_push` tool to automatically sign every git commit with "evolved by {bot_name}". This ensures the agent's identity is clearly attributed in the repository history.
- **Bot Name Git Attribution** — Git commits made by the agent now use the `BOT_NAME` from the environment for the `user.name` configuration.

### Changed
- **System Management Skill Update** — Formally integrated the Signature Mandate into the `system-management-skill`, requiring all `CHANGELOG.md` entries to be signed by the bot.

evolved by Ori

## [0.4.2] - 2026-03-29

### Added
- **File Deletion Support in Evolution Tools** — Upgraded `evolution_commit_and_push` to support a `delete_files` parameter. This allows Ori to permanently remove obsolete or redundant files from both the GitHub repository and the live filesystem during self-evolution.

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
- **Updated System Management Skill** — Expanded "Sandbox Hygiene Rules" with instructions for handling transient build artifacts (`.venv`, `.pytest_cache`, `__pycache__`) admit resolving large commit errors. This ensures future self-evolution cycles handle sandbox clutter correctly.

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
- **SameFileError on sandbox commit** — symlinks (`uv.lock`, `pyproject.toml`, such as existing test files) created during `evolution_verify_sandbox` pytest runs are now cleaned up after verification and skipped during `evolution_commit_and_push` walks.
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
