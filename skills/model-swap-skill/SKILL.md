---
name: model-swap-skill
description: "Model string conventions for set_agent_model — how to format names for Gemini, Claude, OpenRouter, and other providers. Load this any time the user asks to change, swap, set, switch, route, or test a model — to avoid building the wrong model string."
---

# Model Swap — String Format

`set_agent_model` and `verify_model_reachable` take a model string in **`<provider-prefix>/<rest>`** form. Get the prefix right or the probe rejects with `UNKNOWN_PROVIDER`.

## Registered providers

| Prefix | When to use | Example |
|---|---|---|
| `litellm/` | LiteLlm-routed (default for hot-swap). Any model litellm supports — Gemini, Anthropic, OpenAI, Mistral, Cohere, Groq, etc. | `litellm/gemini/gemini-3.1-flash-lite-preview` |
| `gemini/` | Native ADK `Gemini` class. Required for pinned components (google_search, embedding). Same-provider swaps only. | `gemini/gemini-3.1-flash-lite-preview` |
| `anthropic/` | Native ADK `Claude` class. | `anthropic/claude-sonnet-4-6` |
| `openrouter/` | OpenRouter (multi-provider routing through one API). Goes through LiteLlm under the hood. | `openrouter/deepseek/deepseek-v4-flash` |

If you see `UNKNOWN_PROVIDER` in the response, you used a vendor name as the prefix instead of one of the four above.

## OpenRouter format — the most common trap

OpenRouter model IDs on https://openrouter.ai/models look like `vendor/model-name` (e.g. `deepseek/deepseek-v4-flash`, `anthropic/claude-3.5-sonnet`, `meta-llama/llama-3.1-405b-instruct`). To use them in Ori:

```
openrouter/<vendor>/<model>
```

**The `openrouter/` prefix is REQUIRED.** It tells Ori's `PROVIDER_REGISTRY` to route through OpenRouter; without it, the probe rejects the model.

Examples:

| User says | Correct model string |
|---|---|
| "deepseek-v4-flash via openrouter" | `openrouter/deepseek/deepseek-v4-flash` |
| "claude sonnet via openrouter" | `openrouter/anthropic/claude-3.5-sonnet` |
| "gpt-4o via openrouter" | `openrouter/openai/gpt-4o` |
| "llama 405b via openrouter" | `openrouter/meta-llama/llama-3.1-405b-instruct` |
| "grok via openrouter" | `openrouter/x-ai/grok-2` |

**WRONG (probe will reject):** `deepseek/deepseek-v4-flash`, `openai/gpt-4o`, `x-ai/grok-2` — these have no registered provider prefix.

## Same-provider via LiteLlm vs native

If the user just says "use Claude Sonnet 4.6" (no OpenRouter mentioned and they have an `ANTHROPIC_API_KEY` configured), prefer native or LiteLlm-routed:

- `litellm/anthropic/claude-sonnet-4-6` — hot-swappable, recommended
- `anthropic/claude-sonnet-4-6` — native Claude class, locked at construction (can't cross-provider swap later)

Default to `litellm/...` unless the user specifically asks for the native class.

## Pinned components

`google_search` and `embedding` are PINNED to native classes (search grounding + embedding endpoint require it). Attempts to swap them return `COMPONENT_PINNED`. Don't try.

## Procedure

1. Parse the user's intent: which **component** (CoordinatorAgent / DeveloperAgent / KnowledgeAgent / summarizer) and which **provider/model**.
2. If unsure, ASK — don't guess. The list of components: call `list_available_models` to see them.
3. Construct the model string per the prefix table above. **If the user said "via openrouter", the string MUST start with `openrouter/`.**
4. (Optional) Call `verify_model_reachable("<model>")` first to probe without persisting.
5. Call `set_agent_model("<Component>", "<model>")` — runs the probe, persists on success.
6. Confirm to the user with the result. If the probe failed, relay the error verbatim.

## What the user said vs what to build

| User intent | Build |
|---|---|
| "switch coordinator to deepseek-v4-flash via openrouter" | `set_agent_model("CoordinatorAgent", "openrouter/deepseek/deepseek-v4-flash")` |
| "use claude for the developer" | `set_agent_model("DeveloperAgent", "litellm/anthropic/claude-sonnet-4-6")` |
| "test if openrouter/x-ai/grok-2 works" | `verify_model_reachable("openrouter/x-ai/grok-2")` |
| "reset everyone to defaults" | `reset_model_override("")` (empty = all) |
| "what model is the coordinator on?" | `list_available_models()` and read the row |
