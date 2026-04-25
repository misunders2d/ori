# Changelog

## [2.0.0] — Clean ADK 2.0 reimplementation

Branch: `google-adk-2.0-clean` (cut from master `6770893`).
This is a full rebuild on ADK 2.0 idioms — none of the transitional
scaffolding from the prior `google-adk-2.0` migration branch is included.
Same external behaviour (evolution, A2A, DNA exchange, multi-step plans,
multi-channel transport, vault, supervisor lifecycle, multilingual
guardrails). Different internals.

### Architecture

- **Runtime root** is a `Workflow` (`app/workflows/plan_executor.py`).
  Single-turn chat passes through the `plan_completion_check` node and
  terminates immediately. Multi-step plans loop the coordinator via a
  routed edge until `runtime.plan_storage.has_pending_steps` returns
  False — no external re-prompt loop, no continuation gymnastics.
- **Plugins** are the only guardrail attachment point — ten of them in
  canonical order on `App(plugins=[...])`. Logic is inline; no wrapping
  legacy callbacks. Multilingual-safe by construction.
- **Native ADK 2.0 services** wired at the Runner: `OriMemoryService`
  (LanceDB-backed), `OriCredentialService` (vault-backed),
  `FileArtifactService` (filesystem-backed), `DatabaseSessionService`.
- **Plan storage is durable** — SQLite-backed (`data/plans.db`). A bot
  crash mid-plan keeps the in-progress step on disk; the next
  `get_next_step` resumes without double-claiming.
- **A2A is multimodal** — agent card declares text + binary input/output
  modes. Tools `call_friend` / `call_agent` accept `str | types.Content`.
  DNA bundles ride inline as `application/gzip` parts. The legacy public
  download URL flow is gone.
- **OAuth integrations** are a drop-in subsystem — `app/integrations/`
  with a provider ABC, REGISTRY, Google + GitHub examples. Tokens flow
  through `OriCredentialService`. Tools that need an OAuth credential
  declare `auth_config` and ADK plumbs it.
- **Transports** live under `app/transports/<name>/` with a REGISTRY.
  Adding Discord / Slack / a webhook-based bridge is a directory drop +
  a single `register_transport(name)` call.
- **Model provider registry** in `app/util/models.py`. Default `LiteLlm`
  for everything → cross-provider hot-swap is a model-string change.
  Native `Gemini` pinned for `google_search` and `embedding` (search
  grounding + embedding endpoint require it).
- **State schema** is a Pydantic `OriSessionState` attached at the
  Workflow level (App in 2.0 doesn't carry state_schema directly).

### Modules

```
app/
├── agent.py           — App: root_agent (Workflow), plugins, compaction, resumability
├── state.py           — OriSessionState
├── tasks.py           — scheduled / system task entry points (workflow drives the loop)
├── a2a_server.py      — to_a2a wiring + multimodal card + OAuth callback
├── scheduler_instance.py / secure_config.py removed (state moved into runtime/)
│
├── agents/            — coordinator / developer / knowledge
├── plugins/           — 10 plugins, logic inline, _common helpers
├── workflows/         — plan_executor (the root)
├── runtime/           — executor, transport ABC, plan_storage, memory_service,
│                        credential_service, secure_capture, perimeter,
│                        pending_actions, channel_logger, health, backup,
│                        origins, session_signals, oauth_state, auth (TOTP)
├── transports/        — per-transport packages with REGISTRY (telegram, cli)
├── integrations/      — OAuth providers with REGISTRY (google, github)
├── tools/             — agent-facing tools (a2a, planner, memory, model_tools,
│                        integrations, evolution, web, scheduling, etc.)
├── toolsets/          — bundles for agents
└── util/              — config, models (PROVIDER_REGISTRY), totp, schema, telemetry
```

### Removed

- `app/sub_agents/` — folded into `app/agents/`.
- `app/core/` — split into `app/runtime/` (verbatim carry-forward) +
  `app/util/` (utilities). `agent_executor.py` and `memory.py` rewritten.
- `app/callbacks/` — guardrail logic absorbed into the plugin classes.
- `app/app_utils/` — moved to `app/util/`.
- `interfaces/` — replaced by `app/transports/`.
- `force_sync.py` and `tests/test_force_sync.py` — destructive script
  that ran during pytest collection. Never coming back.
- `_drive_plan_to_completion`, `_PLAN_CONTINUATION_PROMPT`,
  `_MAX_PLAN_ITERATIONS` from `app/tasks.py` — workflow handles iteration.
- All `ORI_USE_*` gate flags from the prior migration branch — there's
  one path now.

### Tooling

- `pyproject.toml` pinned `google-adk[a2a]>=2.0.0b1,<3.0.0` with
  `[tool.uv] prerelease = "allow"`.
- `pytest.ini` has `addopts = -m "not infra"` to keep destructive
  infra-marked tests out of default collection.

### Tests

- 49 test files, 222 tests collected, 213 passed, 9 deselected (infra),
  0 errors, 0 collection failures.
- New coverage: state schema, model registry precedence + pinned
  components, durable plan storage (round-trip + crash-resume),
  credential service vault round-trip, memory service LanceDB
  round-trip (infra-marked — needs FastEmbed download), all 10 plugins
  (positive + negative + scoping), OAuth integration registry +
  authorize-url + exchange-code (httpx-mocked), transport REGISTRY +
  multimodal `send_media`, App + Workflow wiring, executor + multimodal
  agent card, OAuth `configure → callback` round-trip.

---

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
