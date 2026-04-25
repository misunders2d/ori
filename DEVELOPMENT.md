# Ori Development Guide

This document is the working reference for extending Ori. Architecture
overview is in `README.md`; full inventory and reasoning is in
`CHANGELOG.md` under the `[2.0.0]` entry.

The platform is intentionally modular. Every extension category below
has a single, well-defined drop-in point. No hidden plumbing: add a
file, register it in one place, done.

---

## Adding a new chat / transport

Drop `app/transports/<name>/` containing:

- `adapter.py` — subclass `app.runtime.transport.TransportAdapter`.
  Implement `platform_name`, `make_session_id`, `make_user_id`,
  `parse_notify_info`, `send_message`, `send_typing`, `send_media`,
  `delete_message`, `download_file`. The Telegram adapter
  (`app/transports/telegram/adapter.py`) is the reference — copy its
  shape and adapt the wire calls.
- `poller.py` (or `chat.py`) — exposes:
  - `is_enabled() -> bool` — env-based gate (e.g. token present).
  - `start_poller(get_runner_fn, process_init_fn) -> coroutine` — the
    asyncio task `run_bot.py` launches.

Register in `app/transports/__init__.py`:
```python
TRANSPORTS = ["telegram", "<name>", "cli"]   # cli stays last (fallback)
```

Tests: copy `tests/test_transports.py`'s adapter pattern. Don't run a
live poller in tests — mock `httpx.AsyncClient.post`.

**Security parity:** if the legacy Telegram poller has a guard the new
transport doesn't have (secret-scrubbing on outbound, mention
requirement, blacklist short-circuit, secure-key capture, etc.), the
new transport is a regression. Audit `app/transports/telegram/poller.py`
top-to-bottom before merging a new transport.

---

## Adding a new OAuth integration

Drop `app/integrations/<provider>/` (or a single `<provider>.py`)
containing a subclass of `app.integrations.base.IntegrationProvider`.
Set the class attributes:

```python
name: str = "myprovider"
default_scopes: tuple[str, ...] = ("read",)
_auth_url: str = "https://myprovider.example/oauth/authorize"
_token_url: str = "https://myprovider.example/oauth/token"
_revoke_url: str = ""   # optional
```

Implement `auth_scheme()` returning an `AuthScheme` (typically `OAuth2`
with the right `OAuthFlowAuthorizationCode`). The base class handles
`authorize_url`, `exchange_code`, `refresh`, `revoke`, `scope_check`
generically over httpx — override only if the provider has quirks.

Register in `app/integrations/__init__.py`:
```python
from app.integrations.myprovider import MyProvider
register_provider(MyProvider())
```

Configure credentials in vault:
```
OAUTH_MYPROVIDER_CLIENT_ID
OAUTH_MYPROVIDER_CLIENT_SECRET
OAUTH_MYPROVIDER_REDIRECT_URI    # optional; default is <A2A_BASE_URL>/oauth/<name>/callback
```

Tools that need the credential declare `auth_config` and ADK plumbs it
through `OriCredentialService`. The OAuth callback handler at
`/oauth/<name>/callback` (in `app/a2a_server.py`) auto-handles the
token exchange and saves under `OAUTH:<name>:<user_id>`.

---

## Adding a new LLM provider

Most additions are one entry in `app/util/models.py`:

```python
PROVIDER_REGISTRY["mistral"] = lambda remainder, opts: LiteLlm(model=f"mistral/{remainder}", **opts)
```

`LiteLlm` already routes to almost every provider via the litellm
library, so a `litellm/<provider>/<model>` model string works without
code changes — just set it as a default or via state hot-swap.

For native (non-litellm) providers, add a factory entry that returns a
`BaseLlm` subclass instance, and add the component to
`MODEL_DEFAULTS`. If the model integration requires native class
behaviour (e.g. native tool grounding), add it to `PINNED_COMPONENTS`
so `set_agent_model` rejects swap attempts at runtime.

---

## Adding a new tool

Drop `app/tools/<domain>.py` with a function (sync or async) that
takes `tool_context: ToolContext = None` as the last parameter.
Return a structured `dict` — convention is
`{"status": "success" | "error", "error_code": ..., "message": ..., ...}`.

If the tool needs an OAuth credential, declare `auth_config`:
```python
from google.adk.auth.auth_tool import AuthConfig
from app.integrations import REGISTRY

@FunctionTool(auth_config=AuthConfig(auth_scheme=REGISTRY["google"].auth_scheme()))
async def list_drive_files(...):
    ...
```

Add the tool to the right toolset bundle (`app/toolsets/<domain>.py`)
or pass it directly to the agent's `tools=[...]` list in
`app/agents/<agent>.py`.

**Multilingual rule:** never regex on English keywords for intent
detection inside a tool. Trust the LLM's intent classification
upstream and the tool's structured input.

---

## Adding a new agent

Drop `app/agents/<name>.py` defining an `LlmAgent` (or `Agent`).
Use `get_model("<ComponentName>")` from `app/util/models.py` and add
the component to `MODEL_DEFAULTS` — that gives admins a hot-swap
target.

Mount the agent either:
- As a `transfer_to_agent` target on the coordinator —
  `app/agents/coordinator.py`'s `sub_agents=[...]`. Multilingual-safe
  via LLM-driven dispatch.
- As a node in a workflow — `app/workflows/<name>.py` edges.

Don't attach `before_*_callback` / `after_*_callback` kwargs.
Cross-cutting concerns belong in plugins.

---

## Adding a new workflow

Drop `app/workflows/<name>.py` defining a `Workflow`. Compose existing
agents and tools as nodes. The plan executor
(`app/workflows/plan_executor.py`) is the reference for the
`coordinator -> check -> coordinator` loop pattern.

If the workflow is the new App root, swap it in
`app/agent.py:app = App(root_agent=<workflow>, ...)`.

---

## Adding a new plugin (cross-cutting hook)

Drop `app/plugins/<name>.py` with a `BasePlugin` subclass:

```python
class MyGuardPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="my_guard")

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        if not _allowed(tool, tool_args, tool_context):
            return {"status": "error", "error_code": "MY_GUARD", "message": "Denied."}
        return None
```

Add to `app/plugins/__init__.py` exports and to the `PLUGINS` list in
`app/agent.py`. **Order matters** — the first plugin to return a value
short-circuits the rest. Document the position you chose with a
one-line comment in `app/agent.py:PLUGINS`.

For per-agent scoping, gate on `tool_context.agent_name` or
`agent.name` inside the hook (see `AdminGatePlugin` for the pattern).

---

## Adding a new skill

Drop `skills/<name>/SKILL.md` (must start with YAML frontmatter — at
minimum `name` and `description`). Optional: `references/` and
`examples/` subdirectories with longer-form material the agent loads
on demand.

Reference from any agent that needs it:
```python
from google.adk.skills import load_skill_from_dir
my_skill = load_skill_from_dir(skills_dir / "my-skill")
```

Add to that agent's `SkillToolset(skills=[...])`.

---

## Adding a new mime/binary type over A2A

1. Add the mime to `defaultInputModes` in
   `app/a2a_server.py:_build_agent_card`.
2. Add a magic-byte signature to
   `app/plugins/binary_content_scanner.py:_MAGIC_BYTES` so the inbound
   safety scanner doesn't reject it as spoofed.
3. If the type is for a specific tool flow (DNA, image attachments,
   etc.), add the inbound handler in the tool that processes it.

---

## State schema additions

`app/state.py:OriSessionState` is the canonical state schema (attached
to the `Workflow` via `state_schema=`). Plugins and tools should
declare new fields here rather than stashing under ad-hoc keys.
`extra="allow"` lets plugins add transient state without a schema
change, but anything load-bearing belongs in the schema.

---

## Plan-and-execute (durable storage)

Plans live in SQLite at `data/plans.db` (managed by
`app/runtime/plan_storage.py`). Crash mid-plan: the in-progress step
stays on disk; the next `get_next_step` call resumes it.

Tools layer (`app/tools/planner.py`) is a thin async wrapper over the
storage. To add a new plan operation, add it to the storage module
first, then expose the tool wrapper. Test the storage in isolation
(see `tests/test_plan_storage.py`) before testing the tool.

The workflow's loop edge (`app/workflows/plan_executor.py`) drives
plans to completion natively. The legacy re-prompt loop in
`app/tasks.py` is gone.

---

## Roadmap

### High priority
- [ ] Migrate evolution-verify retry from `VerifyRetryPlugin` to a
      Workflow-node with `RetryConfig` once the existing dict-based
      failure detection is rewritten as exception-based.
- [ ] Slack transport (referenced in `evolutions/slack-integration/`
      catalog entry; not yet under `app/transports/slack/`).
- [ ] Discord transport.

### Medium priority
- [ ] Cohere / Mistral native model providers (litellm covers them
      already; native is for grounding-style features).
- [ ] BigQuery analytics plugin (ADK 2.0 ships
      `BigQueryAgentAnalyticsPlugin`; opt-in if telemetry is wanted).
- [ ] Memory Bank (Vertex AI) as an alternative to LanceDB —
      `OriMemoryService` is already a `BaseMemoryService` subclass, so
      a swap is a single line in `run_bot.py:get_runner`.

### Completed (Recent Milestones)
- [x] **v0.7.0**: Agent-to-Agent (A2A) Protocol implementation (Ori-Net).
- [x] **v0.7.1**: Robust Tool Confirmation system with human-readable summaries.
- [x] **v2.0.0** (this branch): Clean ADK 2.0 reimplementation. Native
      services, plugin-only guardrails, durable plans, multimodal A2A,
      OAuth integrations subsystem, model hot-swap, modular transports.
