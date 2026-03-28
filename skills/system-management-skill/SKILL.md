---
name: system-management-skill
description: Critical execution rules for the Core Lifecycle Tools that govern the Daemon.
---

# System Management Constraints

The `ori` daemon is a fully integrated, continuously polling worker node. It manages its own persistent execution via four system-critical tools defined in `app/tools/system.py`.


## Core System Tools

1. **`update_self`**: Pulls latest codebase and restarts the node container.
2. **`session_refresh`**: Wipes or summarizes SQLite DB context blocks.
3. **`trigger_rollback`**: Recovers a previously stable git footprint and restarts.
4. **`set_planner_mode`**: Dynamically toggles deep-thinking inference (`BuiltInPlanner`) execution for the current session.

## MANDATORY Security Constraints

1. **Never Bypass Confirmation:** These four tools are fundamentally mapped to the `CoordinatorAgent` as ADK `FunctionTool` wrappers with `require_confirmation=True`. Under NO CIRCUMSTANCES should you alter, remove, or try to bypass the `require_confirmation` wrapper. The system depends on delegating this security authorization step to the human user sitting at the chat interface. DO NOT attempt to execute them implicitly.
2. **Never Remove From Root Agent:** These tools belong permanently bound to the `CoordinatorAgent`. Do NOT attempt to mount them internally into sub-agents unless explicitly architecting a new confirmation matrix.
3. **Runner Lifecycle:** DO NOT attempt to rewrite `run_bot.py`'s daemon lifecycle or memory references without explicit user permission. The async loop handles complex APScheduler and Messenger state interactions precisely, and modifying the polling architecture without extreme caution will break the deployment container loop.
4. **Guardrail Protection:** The guardrails (event callbacks like `before_agent_callback`, `before_model_callback`, `before_tool_callback`, and `after_tool_callback`) are critical for system safety and security. You MUST NOT remove, modify, or try to bypass these guardrails under any circumstances, unless explicitly requested by the user. This includes logic within tools or agent configurations.

Always test any changes that brush up against `system.py` logic thoroughly in your isolated sandbox verification pipeline (`evolution_stage_change` -> `evolution_verify_sandbox`).

## REGRESSION TESTING MANDATE

Every feature, bug fix, or code improvement **MUST** include a corresponding functional test file in the `tests/` directory.

**Testing Protocol:**
1. **Develop Real Tests:** No code change is complete until its corresponding test file (e.g., `tests/test_feature_name.py`) is staged and verified.
2. **Persistence:** These tests must be saved permanently to the repository. They form the core safety net that prevents future updates from breaking existing functionality.
3. **Full Suite Runs:** Every `evolution_verify_sandbox` cycle must invoke the entire existing test suite (`uv run pytest tests`). Any failure in any test (including older ones) must block the commit.
4. **Mocking External Systems:** Use `unittest.mock` to simulate tools, runners, sessions, and events. Tests should verify logic without requiring live API keys or external infrastructure.

## First-Start Setup Flow

On first start with no `ADMIN_PASSCODE` in `.env`, `run_bot.py` auto-generates a random passcode, writes it to `.env`, and prints a setup banner to the server console. This banner shows the passcode and the `/init` command syntax.

**User-facing flow (Telegram):**
1. User sends `/start` → bot explains that a `GOOGLE_API_KEY` is needed and shows the `/init` command format.
2. User sends `/init <PASSCODE> GOOGLE_API_KEY=xxx` → bot authenticates via passcode, writes the key to `.env`, and reloads the runner.
3. Any message sent before the API key is configured gets a clear "not configured yet" response with setup instructions.

**Critical constraints:**
1. **Never change the passcode generation logic** without explicit user permission. Users rely on finding it in the console output or `.env` file.
2. **Never expose the passcode in Telegram messages.** The console and `.env` file are the only authorized locations.
3. **The `/init` command deletes itself from chat** (line 373 in `telegram_poller.py`) since it may contain inline credentials. Do not remove this behavior.

## Origins Protocol

Every Ori instance is a fork of the original upstream repository: `https://github.com/misunders2d/ori`

This daemon is designed to be copied, deployed independently, and evolved as a separate entity. However, each instance retains awareness of its origin and can selectively sync improvements from upstream.

**Capabilities:**
1. **Upstream check:** When the user asks to check for updates, improvements, or security fixes from the original project, use `web_fetch` to read the upstream repository (commits, specific files, or the full repo tree at `https://github.com/misunders2d/ori`). Compare upstream changes against the local codebase via `evolution_read_file`.
2. **Selective adoption:** Present upstream changes to the user as proposals — never auto-merge. The user decides which changes to adopt. Apply accepted changes through the standard evolution pipeline (stage → verify → commit).
3. **Divergence is expected.** Each Ori instance will evolve differently based on its user's needs. Upstream sync is advisory, not mandatory.

**User communication:** If the user asks about updates, origins, or where this bot came from, explain that:
- This bot is built on the Ori framework — a self-evolving autonomous agent originally from `https://github.com/misunders2d/ori`
- It can check the original repo for the latest improvements, security patches, and new features
- The user controls what gets adopted — nothing is applied without approval

**Bot name:** The instance name is stored in `BOT_NAME` in `.env` (defaults to "Ori"). It is loaded into session state as `{bot_name}` and used throughout the agent instructions. Users can rename their bot via `/init <PASSCODE> BOT_NAME=NewName` or by asking the coordinator to update it. The name is cosmetic — it does not affect the internal `app_name` ("ori") used for sessions and databases.

**Signature Mandate:** All git commits and `CHANGELOG.md` entries authored by the agent **MUST** be signed with the phrase "evolved by {bot_name}". 
- The `evolution_commit_and_push` tool handles the git commit signature automatically.
- The Developer Agent is responsible for manually appending "evolved by {bot_name}" to the end of each new entry in `CHANGELOG.md`.
- **CRITICAL:** This signature MUST NOT be used in regular chat messages. It is strictly for technical version control and documentation history.

**Critical constraints:**
1. **Never auto-sync from upstream.** All changes require user confirmation through the standard `require_confirmation` commit flow.
2. **Never overwrite local customizations blindly.** When adopting upstream changes, reconcile them with local modifications — the user's evolution takes priority over upstream defaults.
3. **Always verify upstream code in the sandbox** before committing, just like any other change.

## Sandbox Hygiene Rules

The sandbox (`./data/sandbox/`) uses **symlinks** as bootstrap artifacts during `evolution_verify_sandbox` pytest runs. These symlinks point back to live project files (`pyproject.toml`, `uv.lock`, existing test files) so `uv run pytest` can resolve dependencies.

**Critical constraints:**
1. **Never stage symlinked files.** Only files written via `evolution_stage_change` are real changes. Symlinks are transient scaffolding.
2. **Never copy symlinks back to the live codebase.** Copying a symlink that points to itself causes a `SameFileError` and blocks the commit pipeline.
3. **Symlinks are cleaned up automatically** after pytest verification completes. If you encounter stale symlinks in the sandbox, remove them before committing — they are not your staged work.
4. **If pytest needs additional project files** beyond `pyproject.toml` and `uv.lock`, stage real copies via `evolution_stage_change` rather than creating manual symlinks.
5. **Ignore transient build artifacts:** Directories like `.venv`, `.pytest_cache`, and `__pycache__` are often created during `evolution_verify_sandbox` runs. While the `evolution_commit_and_push` tool is coded to skip these, you must proactively ensure they never leak into the permanent project file structure.
6. **Clean-Before-Commit Protocol:** Before invoking `evolution_commit_and_push`, always inspect the sandbox contents. If dot-folders (like `.pytest_cache`) or virtual environments are present, use a temporary "cleaner" script to delete them. This prevents shell argument length errors and accidental indexing of thousands of library files.
7. **Large Commit Guardrails:** If a commit fails with a `non-zero exit status 1` or mentions internal paths like `.venv/bin/...`, it means the sandbox has accidentally indexed system files. Do NOT try to force the commit. Run a cleanup script first and ensure only your intended code changes are staged.
