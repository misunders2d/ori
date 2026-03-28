# Changelog

All notable changes to the Ori framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/). Version bumps follow [Semantic Versioning](https://semver.org/).

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
- **Context limit crash unrecoverable** — error message now directs users to `/reset` instead of the vague "wait a few minutes".
- **`/start` gave no setup guidance** — now shows full onboarding instructions when unconfigured.
- **"Visit /setup page" referenced nonexistent page** — replaced with actual `/init` instructions.
- **Deploy watcher stuck on git edge cases** — the update loop's git topology analysis could permanently stall on diverged histories, uncommitted local changes, or corrupted HEAD, requiring a manual container restart. Replaced with unconditional `fetch` + `reset --hard origin/master` + `clean -fd`, enforcing the remote as the sole source of truth.

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
