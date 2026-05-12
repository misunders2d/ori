# Thinking Levels

Per-component thinking budget control for the agent tree. Mirrors `MODEL_DEFAULTS` — every component has a default level matching its role, with runtime overrides persisted to `data/thinking_config.json`.

> Source: `app/app_utils/thinking.py`. Applied per-turn (Gemini) by `app/callbacks/guardrails/core.py:prompt_injection_guardrail` and at boot/swap-time (Anthropic via LiteLlm) by `apply_to_agent_tree`.

---

## 1. Why this exists

Gemini 3 Flash/Pro default to `high` thinking. The previous boolean `set_thinking_mode(enabled=False)` set `thinking_config=None`, which does NOT disable thinking — it leaves the model on its default `high`. The toggle was effectively a no-op for Gemini 3, costing real money on thinking tokens for routing agents that don't need to think.

Per-component levels replace the boolean with explicit control. Routing agents drop to `low` (or `minimal`), code/SQL synthesis stays at `medium`, only complex-reasoning roles go `high`.

## 2. Levels (Gemini 3 vocabulary)

| Level     | Gemini behaviour                            | Anthropic budget map | When to use                                |
|-----------|----------------------------------------------|----------------------|--------------------------------------------|
| `minimal` | No thinking for most queries                | thinking off         | CRUD, simple tool execution, summarization |
| `low`     | Minimum latency + cost                      | thinking off         | Top-level routing, pattern-match decisions |
| `medium`  | Balanced — codegen / SQL synth              | budget 4096          | matplotlib codegen, SQL synthesis, self-evolution |
| `high`    | Maximum reasoning depth (Gemini 3 default)  | budget 8192          | Complex multi-step reasoning (rarely justified) |

Thinking tokens are billed as **output** on Gemini ($3/M for Flash). Routing agents on `high` burn output tokens on pattern-match decisions that need none.

## 3. Defaults

`THINKING_DEFAULTS` in `app/app_utils/thinking.py`:

| Component                  | Default   | Rationale                                |
|----------------------------|-----------|------------------------------------------|
| `CoordinatorAgent`         | `medium`  | Top router. Bumped from `low` 2026-05-12 — `low` was too shallow for explicit rule-following (bot hallucinated a self-reboot refusal). |
| `AmazonHeadAgent`          | `medium`  | Domain router. Same rationale as Coordinator. |
| `KnowledgeAgent`           | `low`     | A2A messaging.                           |
| `AmazonAgent`              | `minimal` | Keepa/SP-API/H10 CRUD.                   |
| `AmazonMemoryAgent`        | `minimal` | Neo4j CRUD.                              |
| `AmazonWorkspaceAgent`     | `minimal` | Drive/Sheets/Calendar CRUD.              |
| `ClickUpAgent`             | `minimal` | Task CRUD.                               |
| `google_search`            | `minimal` | Query-gen.                               |
| `AmazonDataAnalystAgent`   | `medium`  | matplotlib + pptx codegen.               |
| `BigQueryAgent`            | `medium`  | SQL synthesis.                           |
| `youtube_summarizer`       | `minimal` | One-shot transcript summarization.       |
| `summarizer`               | `minimal` | Context compaction.                      |
| `session_summarizer`       | `minimal` | Session compaction.                      |
| `DeveloperAgent`           | `medium`  | Self-evolution code work (on Sonnet).    |

## 4. Runtime control

From inside chat (admin):

```
set_thinking_level(component="CoordinatorAgent", level="minimal")
reset_thinking_level(component="CoordinatorAgent")
list_thinking_levels()
```

Persists to `data/thinking_config.json` (`{"levels": {component: level}}`). Survives restart. Live agent tree picks up immediately — Gemini agents per-turn via the callback, LiteLlm-backed (Anthropic) agents via `apply_to_agent_tree`.

## 5. Resolution order

1. Per-component override in `data/thinking_config.json`
2. `THINKING_DEFAULTS` in `app/app_utils/thinking.py`

Independent per component. Setting `set_thinking_level("CoordinatorAgent", "minimal")` only affects the coordinator; sub-agents stay on whatever they had.

## 6. Provider mechanics

### Gemini (per-turn)

`prompt_injection_guardrail` (in `app/callbacks/guardrails/core.py`) sets `llm_request.config.thinking_config = ThinkingConfig(thinking_level=level)` on every request. Fires on every turn for every Gemini agent.

`ThinkingConfig.thinking_level` and `ThinkingConfig.thinking_budget` are mutually exclusive per Google docs — we use `thinking_level` (Gemini 3 native vocabulary).

### Anthropic / LiteLlm (long-lived kwargs)

Anthropic uses `thinking={"type":"enabled","budget_tokens":N}` as a completion kwarg, not a per-call request field. `apply_to_agent_tree` walks every LiteLlm-backed model on the agent tree and mutates `model._additional_args["thinking"]` to match the per-agent level. Called at boot (`app/agent.py:56`) and after every `set_thinking_level` / `reset_thinking_level` call.

The level → budget map is `_ANTHROPIC_BUDGET_BY_LEVEL` (minimal/low → off; medium → 4096; high → 8192).

## 7. Deprecation note

The old `set_thinking_mode(enabled, budget_tokens)` global toggle remains as a backward-compat shim. It applies a blanket policy (all components `low` if disabled, `medium`/`high` if enabled). New code and the new control surface should use `set_thinking_level(component, level)`.

`data/thinking_config.json` schema migrated: legacy `{enabled, budget_tokens}` shape is silently ignored on load (defaults apply). New schema is `{"levels": {component: level}}`.

## 8. Cost lever sizing

Thinking tokens on Flash bill at $3/M (output rate). A routing agent on `high` thinking may emit 500-2000 thinking tokens per turn. Across the 3 Flash router agents (Coordinator, AmazonHead, Knowledge) at ~200 turns/day × 30 days ≈ 18K turns × 1500 thinking tok = 27M tokens/month × $3/M = **$80/mo on routing thinking** that adds no value to pattern-match decisions.

Dropping those three to `low` cuts thinking tokens to a few hundred at most. Estimated saving: $60-70/mo at current usage. Scales linearly with turn volume.

For verification, log `usage_metadata.candidates_token_count` minus the visible response text length — the delta is the thinking-token output.
