# Model Hot-Swap

How to change which LLM Ori uses for any given role — at runtime, per component, without restarting (mostly), without editing code.

> Source: `app/app_utils/models.py`, `app/app_utils/model_config.py`, `app/tools/model_tools.py`. Skill: `skills/model-swap-skill/SKILL.md`.

---

## 1. What a "component" is

A component is a named slot that resolves to a model string. There are 15 today (see `MODEL_DEFAULTS` in `app/app_utils/models.py:24-40`):

| Component | Default |
|---|---|
| Component | Default | Tier | Why |
|---|---|---|---|
| `CoordinatorAgent` | `google/gemini-3-flash-preview` | Flash | top router, judgment calls |
| `DeveloperAgent` | `openrouter/anthropic/claude-sonnet-4.6` | Sonnet | writes its own code (irreversible); Opus 4.7 was default until 2026-05-12 when a single trivial model-name rename cost ~$3 (11+ turns × ~35K input tokens × \$15/M Opus input rate, no prompt-caching on LiteLLM path). Sonnet 4.6 is ~5× cheaper input + output, near-equal tool-calling quality on code. Escalate to Opus only for hard multi-file refactors: `/models set DeveloperAgent openrouter/anthropic/claude-opus-4.7`. |
| `KnowledgeAgent` | `google/gemini-3-flash-preview` | Flash | research + summarisation |
| `AmazonDataAnalystAgent` | `google/gemini-3-flash-preview` | Flash | matplotlib codegen + statistical analysis |
| `BigQueryAgent` | `google/gemini-3-flash-preview` | Flash | SQL synthesis, business reasoning |
| `youtube_summarizer` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | one-shot transcript summarisation, bulk input + short output; moved off Flash 2026-05-12 (~3× cheaper, quality parity on long transcripts) |
| `AmazonHeadAgent` | `google/gemini-3-flash-preview` | Flash | routes every Amazon request; Lite mis-routed in production (2026-05-11) so kept on Flash |
| `AmazonAgent` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | Keepa / SP-API / H10 tool execution |
| `AmazonMemoryAgent` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | graph CRUD + memory queries |
| `AmazonWorkspaceAgent` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | Drive / Sheets / Calendar CRUD |
| `ClickUpAgent` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | task CRUD |
| `google_search` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | search-query generation |
| `summarizer` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | context compaction |
| `session_summarizer` | `google/gemini-3.1-flash-lite-preview` | Flash-Lite | session compaction |
| `embedding` | `google/gemini-embedding-001` | Embedding | semantic search vectors |

Cost-saving rationale: Flash-Lite is ~50% of Flash on Google direct pricing. The Lite components do tool-routing and CRUD where deep reasoning isn't needed — `plan_step_enforcer` hard-blocks any out-of-step tool call so a weaker model can't wander off-plan. Components that DO need reasoning (CoordinatorAgent's top routing, DeveloperAgent's self-modifying code, BigQueryAgent's SQL, DataAnalyst's matplotlib codegen, KnowledgeAgent's research, YouTube transcript summarisation) stay on Flash.

Rollback a single Lite component if quality drops: `/models set <AgentName> google/gemini-3-flash-preview` (hot-swap, no restart). Rollback all to Flash: edit `MODEL_DEFAULTS` directly + commit.

`VALID_COMPONENTS = frozenset(MODEL_DEFAULTS.keys())` enforces the set. Adding a new component means adding a new entry to `MODEL_DEFAULTS`; you cannot `set_model` on an unknown name.

## 2. Model string format

`<provider>/<model_name>`, e.g. `google/gemini-3-pro-preview`, `anthropic/claude-sonnet-4-6`, `openrouter/deepseek/deepseek-chat`.

Supported providers (`PROVIDER_API_KEYS`):

| Provider | API-key env var |
|---|---|
| `google` | `GOOGLE_API_KEY` (or Vertex AI via `GOOGLE_GENAI_USE_VERTEXAI=TRUE` + ADC) |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `openrouter` | `OPENROUTER_API_KEY` |

Provider routing happens inside `get_model(component)` — Google goes to ADK's native Gemini class, Anthropic via LiteLlm (or Vertex if Vertex mode is on), OpenRouter via LiteLlm with the `openrouter/` prefix.

### 2.1 Anthropic prompt caching (OpenRouter)

For any OpenRouter model containing `claude` in the name (case-insensitive), `_build_model` injects `extra_body={"cache_control": {"type": "ephemeral"}}`. LiteLLM's OpenRouter handler forwards `extra_body` keys into the request body root, and OpenRouter's auto-caching marks the last cacheable block — caching the entire prefix (system + tools + history) for 5 minutes.

**Anthropic-direct path:** `anthropic/claude-*` does NOT get caching wired (LiteLLM's anthropic handler needs per-message `cache_control` injection, which would require a 50-LOC `LiteLlm` subclass to mutate messages pre-call). To prevent silent ~5-10× cost regressions, `_build_model` auto-redirects `anthropic/claude-*` through OpenRouter when `OPENROUTER_API_KEY` is set, logging the redirect at INFO level. If only `ANTHROPIC_API_KEY` is set, the request goes direct with a WARNING log noting the missing cache wiring.

Anthropic pricing on cached prefixes:
- Cache write: 1.25× input rate (one-time, ~$3.75/M for Sonnet 4.6)
- Cache read: 0.1× input rate (~$0.30/M for Sonnet 4.6)
- Min cacheable block: 2048 tokens (Sonnet 4.6), 4096 (Opus 4.7)

DeveloperAgent has ~25-30K of stable system + tool schemas, well above min. Across a multi-turn flow, every turn after the first reads cache at 10% input cost — ~90% savings on the static portion.

Verify in OpenRouter activity logs: `prompt_tokens_details.cached_tokens > 0` on second+ turns within 5 min.

Caveat: Gemini caches automatically without a marker; we only inject for Claude. DeepSeek and others on OpenRouter handle caching server-side — no client config needed.

## 3. Resolution order

`get_model_string(component)`:

1. `os.environ[f"MODEL_{COMPONENT.upper()}"]` — highest priority, runtime hot-swap.
2. `data/model_config.json:assignments[component]` — persisted overrides.
3. `MODEL_DEFAULTS[component]` — code default.

Per-component, independent. Setting `MODEL_COORDINATORAGENT=anthropic/claude-sonnet-4-6` only changes the coordinator; sub-agents stay on whatever they had.

## 4. Persistence (`data/model_config.json`)

```json
{
  "assignments": {
    "CoordinatorAgent": "anthropic/claude-sonnet-4-6",
    "DeveloperAgent": "openrouter/anthropic/claude-3-haiku"
  },
  "model_cache": {
    "google":    {"models": [...], "updated": "2026-05-11T..."},
    "anthropic": {"models": [...], "updated": "2026-05-11T..."},
    "openrouter":{"models": [...], "updated": "2026-05-11T..."}
  }
}
```

- `assignments` — what `/models set` writes; what `set_model(component, str)` mutates.
- `model_cache` — list of available models per provider, fetched on demand. Used by `list_available_models` and the setup wizard.

`set_assignment(component, model_str)` in `app/app_utils/model_config.py`:

1. Validates `component in VALID_COMPONENTS`.
2. Normalizes the model string.
3. Atomic write to `data/model_config.json` (tempfile + `os.replace`).
4. Also sets `os.environ[f"MODEL_{COMPONENT.upper()}"]` so the change is visible to the running process.

`reset_model(component)` removes both the env var and the file entry — component falls back to the default.

## 5. Startup rehydration (Phase 2 of hardening)

Today the env is set only when a tool calls `set_model`. If the bot is restarted between sessions, the env is empty and the resolution falls through to the file. That works for components that resolve their model lazily (each `get_model_string` call re-reads the file), but **fails for components that are constructed once at module import** — they capture the default before the file lookup.

After Phase 2, `run_bot.py` calls `hydrate_model_env()` **before any agent module imports**:

```python
def hydrate_model_env() -> int:
    """Read data/model_config.json and seed os.environ[MODEL_*] for every
    persisted assignment. Call this before importing any agent module."""
    cfg = _read_model_config()
    count = 0
    for component, model_str in (cfg.get("assignments") or {}).items():
        if component in VALID_COMPONENTS:
            os.environ[f"MODEL_{component.upper()}"] = model_str
            count += 1
    return count
```

Result: every agent constructed afterward sees the override via env (highest-priority resolution path), independent of whether it re-reads the file later.

## 6. Tooling

| Tool | Purpose | File |
|---|---|---|
| `list_available_models(provider?)` | List models from a provider (cached in `model_config.json`, refreshable) | `app/tools/model_tools.py` |
| `set_agent_model(component, model_str)` | Validate + assign + persist + set env | `app/tools/model_tools.py` |
| `reset_model_override(component)` | Clear an override; component returns to default | `app/tools/model_tools.py` |
| `get_llm_provider(component)` | Read the current resolved model string | `app/tools/model_tools.py` (alias of `get_model_string`) |

The agent-facing flow is documented in `skills/model-swap-skill/SKILL.md`. Setup-wizard collects provider API keys interactively (`app/interfaces/setup_wizard.py`).

## 7. Validation + auto-repair

Probing whether a model is actually reachable is **not done today** (Phase 2 stops at rehydration). A future addition (deferred — see plan §2 for the option we chose against) would `litellm.completion(..., max_tokens=1)`-ping each assignment at startup and log unreachable ones.

What IS done (added later as a focused fix):

- `set_model(component, model_str)` performs a **provider-key preflight** before persisting. If the provider's API key (or Vertex flag) is missing from the environment, the call raises with a clear error instead of writing an override the runtime can't honour.
- `get_model(component)`'s fallback path **auto-repairs** when the build fails: it clears the bad override from `data/model_config.json` AND `os.environ` before returning the default-built model. Otherwise the persisted bad override would re-hydrate on every restart, the build would keep falling back, and `state_setter` would emit a permanent `Cross-provider hot-swap requested for X (a -> b). Takes effect after restart` warning that no restart could reconcile.

The repair logs a single WARNING at boot so operators see why the override was discarded. After the first run, the warning stops firing.

## 8. Global thinking on/off switch

Independent of model assignments, every agent's "extended thinking" can be toggled globally:

- File: `data/thinking_config.json` (`{"enabled": bool, "budget_tokens": int}`), persisted across restarts.
- Tool: `set_thinking_mode(enabled, budget_tokens=4096)` — admin-gated, atomic write.
- Application:
  - Gemini agents — per-turn `llm_request.config.thinking_config` set in `state_setter` (`app/callbacks/guardrails.py`). On = leave default, off = None.
  - LiteLlm agents (Anthropic via OpenRouter etc.) — `apply_to_agent_tree(root_agent)` walks every sub-agent and mutates each LiteLlm instance's `_additional_args["thinking"]` in-place. Runs at boot (`app/agent.py`) and after each toggle so the change is hot.
- Thoughts NEVER reach Slack/Telegram/A2A regardless of the flag — `extract_agent_response` (`app/core/agent_executor.py`) filters `Part(thought=True)` parts and the LiteLlm wrapper's `thinking_blocks` get normalised into the same shape upstream.

See `app/app_utils/thinking.py` for the storage + apply helpers.

For now: if you set a typoed model name, the failure surfaces only when the agent next tries to call it — typically a 4xx from the provider. That message is relayed to the user verbatim per the `relay_error` rule in `AI_EDITS.md`.

## 8. Common operations

### Swap the Coordinator to Claude

```
/models set CoordinatorAgent anthropic/claude-sonnet-4-6
```

Equivalent to: `set_agent_model("CoordinatorAgent", "anthropic/claude-sonnet-4-6")`. After Phase 2, this survives `/reset session` and process restart.

### Roll all components back to defaults

```
/models reset
```

Calls `reset_all_models()` — clears every `assignments[*]` entry and `MODEL_*` env var.

### Inspect current assignments

```
/models
```

Or programmatically: `get_all_model_strings()` returns `{component: model_str}` for every component (resolved through the full hierarchy).

### Add a new component

1. Append to `MODEL_DEFAULTS` in `app/app_utils/models.py`.
2. Either reference it from `get_model("MyNewComponent")` in code, or pass it to LLM-using callsites.
3. Run `uv run python scripts/gen_docs.py` so the docs reflect the new component.

Don't bypass `MODEL_DEFAULTS` and hardcode the model string somewhere downstream — that breaks the hot-swap contract and the user won't be able to switch it later.
