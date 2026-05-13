# Contract-driven scheduling

> Why this exists: scheduled tasks used to drift between authoring and
> fire time. The agent would store a generic `task_prompt` ("Execute
> Monday Pilot"), and at fire time it would reconstruct content from
> memory — fuzzy recall, sub-agent narration leaks, wrong format. The
> 2026-05-11 audit caught this and led to the contract architecture
> described here.

## TL;DR

Every recurring/scheduled task is described by a **frozen contract**:
inputs to gather, reasoning steps (LLM calls with schema-locked
output), and emit adapters (deterministic side effects). Authoring is
conversational — the bot drafts the contract dict with the user;
freezing hashes it; firing executes it literally.

**Author once**, **dry-run for review**, **freeze + hash**, **schedule
once**, **fires deterministically every time**.

## Five-stage envelope

```
GATHER → REASON → MATERIALIZE → VALIDATE → EMIT
```

Every task walks these five stages. Per-task variability lives only in
slot values; the structure never changes.

| Stage | Determinism | Detail |
|---|---|---|
| **GATHER** | deterministic, NO LLM | typed input loaders (BigQuery, Keepa, web search, graph query, …) |
| **REASON** | LLM calls, 0..N | each step has `entry_agent`, tool whitelist, output schema |
| **MATERIALIZE** | deterministic | structured outputs stored under `state[step.id]` |
| **VALIDATE** | mechanical | JSON Schema or text predicates; one retry on fail; then `on_failure` |
| **EMIT** | deterministic, NO LLM | side-effect adapters (slack_post, sheet_append, drive_doc_fill, …) |

## Authoring UX

The agent (CoordinatorAgent + ContractToolset) handles all the wiring.
You describe the task in natural language; the bot drafts the
contract dict, calls `contract_dry_run` to simulate, shows the
simulated output, iterates with you, then freezes + schedules.

Example flow:

```
USER: schedule a daily FBA news digest at 9am Kyiv to #fba_updates

BOT (drafts contract):
  inputs: [web_search, graph_query for KB]
  reasoning: [one LLM call with structured output schema for top items]
  emit: [slack_post, sheet_append for dedup log]

BOT: contract_dry_run(spec) → simulated post:
     "*FBA news — past 5 days* (2026-05-11)
      *5/5 — Amazon raises FBA storage fees ..."

USER: looks good, but lower importance threshold to 3.

BOT (revises): updates output.schema.top_items.importance.minimum → 3
BOT: contract_dry_run(...) → new preview
USER: approve

BOT: contract_freeze(spec)         → hash a1b2c3...
BOT: contract_schedule("fba_news") → wired to APScheduler at contract:fba_news
```

## Contract schema

See `app/contracts/schema.py` for the canonical Pydantic models. Top-level
shape:

```yaml
id:           string              # snake_case, globally unique
version:      int                 # auto-managed by the store
hash:         string              # SHA-256, populated at freeze time
description:  string              # human readable
author:       string              # user id
parent_hash:  string | null       # link to previous version (revisions)
trigger:      Trigger             # cron | on_demand | event
inputs:       [InputSpec]         # deterministic loaders, 0..N
reasoning:    [ReasoningStep]     # LLM calls, 0..N
emit:         [EmitStep]          # 1..N side effects
acceptance:   Acceptance          # global post-reason / pre-emit checks
on_failure:   FailureAction       # alert_admin | abort_silent | retry_later
enforcement:  EnforcementMode     # STRICT (default; only mode that fires)
```

### Reasoning step shape

```yaml
- id:                 string             # snake_case; state[id] holds the output
  description:        string             # human label
  entry_agent:        string             # sub-agent name (e.g. AmazonHeadAgent)
  transfers_allowed:  [string]           # sub-agents this step may transfer to
  tools:              [string]           # hard tool whitelist
  model:              string | null      # hot-swap model key, else entry_agent's default
  user_template:      string             # rendered against state at fire time
  output:
    type:             "json" | "text" | "none"
    schema:           {...}              # JSON Schema (for json)
    constraints:      [string]           # for text: "min 200 chars", "contains 'X'", …
  retry:
    on_validation_fail: int              # default 1
    on_tool_error:     int               # default 2
  max_tool_calls:     int                # default 20
```

### Emit step shape

```yaml
- id:                 string | null
  adapter:            string             # registered emit adapter name
  args:               {...}              # template-rendered against state
  gate:                                  # optional pre-emit check
    type:             string             # registered gate name (e.g. sheet_dedup)
    args:             {...}
  abort_on_gate_fail: bool               # default false (skip just this emit)
```

## Loaders (GATHER)

Registered in `app/contracts/loaders.py`. Each loader is a coroutine
`(rendered_args, state) → JSON-serialisable value`.

| Loader | Status | Args |
|---|---|---|
| `static_param` | ✅ live | `value` (any) |
| `web_search` | ✅ V1 (single URL) | `url` |
| `graph_query` | ✅ live | `cypher`, optional `params` |
| `memory_search` | ✅ live | `query`, optional `namespace`, `limit` |
| `sheet_read` | ✅ live (2026-05-12) | `spreadsheet_id` (ID or URL), optional `range` (default `"Sheet1"`). Returns list of rows. Per-author OAuth. |
| `drive_doc_read` | ✅ live (2026-05-12) | `doc_id` (ID or URL). Exports as text/plain via Drive `files.export`. Per-author OAuth. |
| `bigquery_query` | ✅ live (2026-05-12) | `sql`, optional `params` ({name: value}), optional `project_id`. Service-account auth (`BQ_GCP_SERVICE_ACCOUNT_INFO`). Returns list of dicts. |
| `keepa_get_history` | ✅ live (2026-05-12) | `asin`, optional `domain` (default 1=US). Returns the cached lightweight summary from `keepa_fetch_product`. |

> Every P7 placeholder was lit up on 2026-05-12 — production AI-Pilot
> contracts had been silently FATALing for weeks because they
> referenced `sheet_append` / `slack_post_message` against
> placeholder-only registrations. The bug class is gone:
> implementations are real, validator rejects unknown names at
> freeze, alerts route to ADMIN_USER_IDS even with empty `notify`.

## Emit adapters (EMIT)

Registered in `app/contracts/emit.py`.

| Adapter | Status | Args |
|---|---|---|
| `slack_post` | ✅ live | `channel`, `content`, optional `thread_ts` |
| `telegram_dm` | ✅ live | `user_id`, `text` |
| `sheet_append` | ✅ live (2026-05-12) | `spreadsheet_id` (ID or URL), `row` (list), optional `range` (default `"Sheet1"`). Per-author OAuth. Always appends — never overwrites. |
| `drive_doc_fill` | ✅ live (2026-05-12) | `doc_id` (ID or URL), `fields` ({name: value}). Replaces `{{name}}` placeholders literally. Per-author OAuth. |
| `memory_update` | ✅ live (2026-05-12) | `namespace`, `text`, `short_description`, `category`, optional `tags`, `related_*`, `force_create`. Author = contract author. |
| `email` | ❌ deferred | Requires adding `gmail.send` to OAuth scopes + user re-auth. Currently UNREGISTERED — validator rejects any contract using it. |

> **Adapter name ≠ tool name.** The Slack adapter is `slack_post`,
> NOT `slack_post_message` (the latter is the underlying Python tool
> function). Same split for `telegram_dm` vs `telegram_send_dm`.
> Authoring tools (`contract_draft_validate`, `contract_dry_run`,
> `contract_freeze`) and `contract_store.freeze` now hard-validate
> every `emit[].adapter`, `emit[].gate.type`, and `inputs[].loader`
> against the live registries via
> `app/contracts/validation.py:validate_against_registries`. A bad
> name fails AT AUTHORING TIME with a list of the known names —
> never again at fire time. Production proof 2026-05-12: 5
> `ai_pilot_*_v2` contracts authored with `adapter:
> "slack_post_message"` FATALed silently every fire for weeks.

> **Adapter ARG names are validated too.** As of 2026-05-13 every
> `@register_adapter` / `@register_gate` / loader `@register` call
> publishes a schema (`required`, `optional`, `aliases`) the author
> validator enforces. Missing required args → reject. Unknown args
> → reject with a `Did you mean X` hint when a close match exists.
> Aliases honour adapter-side flexibility (`sheet_append` accepts
> `spreadsheet_id` OR `source`). See
> `app/contracts/validation.py:validate_adapter_arg_shapes`.
> Production proof 2026-05-13: `ai_pilot_wed_v3` was frozen with
> `args.text` (a Slack tool kwarg name) where `slack_post` requires
> `args.content`. The contract scheduled, fired, failed with
> `slack_post requires args.channel and args.content`, emit_count
> ended at 0, and the per-contract `notify=[]` meant no admin saw
> the alert. The new validator catches this class at freeze.

## Gates

Pre-emit checks. The most common is `sheet_dedup` — read a tracking
sheet, fail if today's row already exists. With `abort_on_gate_fail:
true`, a failing gate aborts the entire contract fire (default: skip
just that one emit, keep going).

| Gate | Status | Purpose |
|---|---|---|
| `sheet_dedup` | ✅ live (2026-05-12) | reject if `args.key` already in column A of `args.source` sheet. Per-author OAuth. Fails-closed: if the read errors, the gate refuses the emit. |
| `always_pass` | ✅ | explicit "we chose no gating here" marker (testing) |
| `always_fail` | ✅ | testing the abort path |

## Templating

Loader args, `user_template` strings, and emit args all support
placeholder substitution before fire time:

| Placeholder | Resolves to |
|---|---|
| `{name}` | `state["name"]` |
| `{name.key}` / `{name.k.sk}` | nested dict descent |
| `{name[0]}` / `{items[2].title}` | list subscript |
| `{today}` | YYYY-MM-DD (UTC) |
| `{today-Nd}` / `{today+Nd}` | N days back / forward |
| `{now}` | ISO-8601 timestamp (UTC) |

Unresolved placeholders raise `TemplateError` at fire time and route
through `on_failure`. No silent fallbacks — the whole point is to
surface drift early.

## Storage + integrity

```
data/contracts/
  <contract_id>/
    index.json                     ← version chain (latest first)
    v1__<hash[:12]>.json           ← frozen body
    v2__<hash[:12]>.json
    ...
data/contract_audit/
  <contract_id>/
    <YYYYMMDDTHHMMSSZ>_<uuid>.jsonl   ← per-fire audit trail
```

The frozen body is written once and never edited in place. Revisions
produce new version files; old versions remain loadable for audit.

Every load verifies `sha256(canonical_body) == hash`. Any drift
(manual edit, partial write, hostile tampering) aborts the fire with
`ContractHashMismatch`.

## Step enforcement guarantee

Once a contract is frozen, the executor walks its steps **literally**:

- Steps **cannot be skipped** — every entry in `reasoning[]` produces
  a validated output stored in state.
- Steps **cannot be mocked at fire time** — dry-run uses
  `mock_inputs` parameter; real fires never substitute mock data.
- Steps **cannot be discarded** — every step's output is captured in
  `state[step.id]` and persisted to the audit log.
- Tool calls **cannot escape the whitelist** — the
  `contract_step_enforcer` callback (P3+ extension to
  `plan_step_enforcer`) hard-blocks anything outside `step.tools`.
- Sub-agent transfers happen **inside** the worker session but never
  reach the user channel. Only emit adapters touch the outside world.
- Schema mismatches **retry exactly once** with the validation error
  as feedback; if still bad, `on_failure` fires — no partial emit.

Step enforcement strictness is captured by `Contract.enforcement` —
`STRICT` is the only production mode. The worker refuses to fire a
`PERMISSIVE` contract.

### Model temperature per family

The reasoning-step LLM call goes through LiteLLM (one code path per
provider). Temperature is **provider-conditional**:

- **Gemini 3 family** (`gemini-3-flash-preview`, `gemini-3-pro`, …) —
  temperature is **omitted**. Google's release notes (echoed by
  LiteLLM's
  `vertex_and_google_ai_studio_gemini.py:1009` runtime warning) state
  that `temperature < 1.0` on Gemini 3 can cause **infinite loops,
  degraded reasoning performance, and failure on complex tasks**. We
  let the provider apply its native default (1.0).
- **Everything else** (Anthropic Sonnet/Opus, Gemini 2.x, OpenRouter
  passthroughs) — `temperature=0.2`. Sub-sampled completions converge
  faster on structured-output schemas and let retry-with-feedback
  produce a stable target.

Implemented in `app.contracts.worker._invoke_llm_for_step` (the only
site that picks temperature). Authoring does **not** expose
`temperature` as a contract field — keeping it provider-rule-driven
makes the per-fire behaviour deterministic.

## Coexistence with legacy scheduled jobs

The legacy `schedule_recurring_task` / `schedule_one_off_task` path
remains operational. Contract jobs use a **different** APScheduler id
prefix (`contract:<id>` vs `cron_<random>`) and a different fire
callback (`run_contract_fire` vs `run_scheduled_task`), so the two
coexist without stepping on each other.

Migration is opt-in, one job at a time:

```
agent: contract_from_existing("cron_79d58bac")
   → returns a draft contract spec approximating the legacy job
agent / user: review, tighten (real inputs, real reasoning, real
   templates), dry-run, freeze, schedule
agent: delete_scheduled_task("cron_79d58bac")   ← only after the new
   contract is firing correctly
```

No bulk auto-migration. The user keeps full control of when each job
moves over.

## Failure modes + alerts

`on_failure` defaults to `alert_admin` + `abort=true`:

- Reasoning step exhausts retries → DM the listed admin user IDs with
  the contract id, the error, and the audit log path. The contract
  does NOT emit anything.
- Pre-emit gate fails AND `abort_on_gate_fail=true` → same path.
- Emit adapter raises → same path.
- Hash drift between load and execute → same path.

Audit log lives at `data/contract_audit/<id>/<fire_id>.jsonl` and
captures one event per phase (input fetch, reasoning attempt, gate
result, emit success/fail, on_failure invocation).

## Worked examples

See `examples/contracts/`:

- `ai_pilot_monday.json` — static recurring (no LLM). Sheet dedup +
  Slack post + sheet log.
- `fba_news_digest.json` — one-call reasoning with structured output.
  Web fetch + graph query + Gemini Flash + slack_post.

The 30-step ASIN audit contract is a follow-up — it requires
sub-agent transfers within a single reasoning step, which V1 of the
worker doesn't yet support cleanly. (V1 already executes 0..N
sequential reasoning steps, so the audit can be expressed as 30
chained single-step reasoning entries with `entry_agent` set per
step. Sub-agent transfers WITHIN a step land in V2.)

## Authoring tools — quick reference

All exposed via `ContractToolset` on CoordinatorAgent.

| Tool | Use |
|---|---|
| `contract_draft_validate(spec)` | catch schema errors before showing the user |
| `contract_dry_run(spec, mock_inputs?)` | simulate one fire, return rendered emit args |
| `contract_freeze(spec)` | persist + hash; refuses non-STRICT |
| `contract_schedule(id)` | wire to APScheduler (or report on_demand / event) |
| `contract_unschedule(id)` | remove from APScheduler (body stays on disk) |
| `contract_revise(id, new_spec)` | freeze a new version with `parent_hash` set |
| `contract_list()` | every contract on disk + scheduler state |
| `contract_inspect(id, version?)` | full body for review |
| `contract_from_existing(job_id)` | draft a spec from a legacy job (migration helper) |
