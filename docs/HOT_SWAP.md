# Model Hot-Swap

How to change which LLM Ori uses for any given role — at runtime, per component, without restarting (mostly), without editing code.

> Source: `app/app_utils/models.py`, `app/app_utils/model_config.py`, `app/tools/model_tools.py`. Skill: `skills/model-swap-skill/SKILL.md`.

---

## 1. What a "component" is

A component is a named slot that resolves to a model string. There are 15 today (see `MODEL_DEFAULTS` in `app/app_utils/models.py:24-40`):

| Component | Default |
|---|---|
| `CoordinatorAgent` | `google/gemini-3-flash-preview` |
| `DeveloperAgent` | `openrouter/anthropic/claude-opus-4.7` |
| `KnowledgeAgent` | `google/gemini-3-flash-preview` |
| `ClickUpAgent` | `google/gemini-3-flash-preview` |
| `AmazonHeadAgent` | `google/gemini-3-flash-preview` |
| `AmazonAgent` | `google/gemini-3-flash-preview` |
| `AmazonMemoryAgent` | `google/gemini-3-flash-preview` |
| `AmazonWorkspaceAgent` | `google/gemini-3-flash-preview` |
| `AmazonDataAnalystAgent` | `google/gemini-3-flash-preview` |
| `BigQueryAgent` | `google/gemini-3-flash-preview` |
| `google_search` | `google/gemini-3-flash-preview` |
| `summarizer` | `google/gemini-3.1-flash-lite-preview` |
| `session_summarizer` | `google/gemini-3.1-flash-lite-preview` |
| `embedding` | `google/gemini-embedding-001` |
| `youtube_summarizer` | `google/gemini-3-flash-preview` |

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

## 7. Validation

Probing whether a model is actually reachable is **not done today** (Phase 2 stops at rehydration). A future addition (deferred — see plan §2 for the option we chose against) would `litellm.completion(..., max_tokens=1)`-ping each assignment at startup and log unreachable ones.

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
