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

### Schedule swap is atomic — no pre-remove

``schedule_contract`` registers / revises a contract via
``scheduler.add_job(..., replace_existing=True)``. Earlier code
called ``scheduler.remove_job(job_id)`` first; that was redundant
(``replace_existing`` already overwrites the row in the
SQLAlchemyJobStore atomically) and opened a tiny race where an
in-flight fire could trigger between the remove and the add. The
pre-call has been dropped.

### Boot order: scheduler resume after transports

``run_bot.main`` now calls ``scheduler.start(paused=True)`` and
schedules a 1.5 s coroutine that calls ``scheduler.resume()`` after
the Slack / Telegram pollers have had time to ``register_adapter``.
Without that pause, an overdue contract / legacy job whose
``next_run_time`` had already passed during downtime would fire AS
SOON AS the scheduler started (driven by the SQLAlchemyJobStore +
``misfire_grace_time=3600``) and try to deliver via
``get_adapter("slack" | "telegram")`` before either poller had
registered. Net effect: a pre-2026-05-14 startup with a missed fire
in the grace window would land in a broken-transport error path
that admins couldn't see. With the paused-start change, every
overdue job's first delivery attempt finds its adapter.

### Deterministic delivery target (LLM may never pick where)

``slack_post.args.channel`` and ``telegram_dm.args.user_id`` may be:

  * a **literal** string (``"C012ABCDE"``, ``"#general"``,
    ``"330959414"``), or
  * a ``{placeholder}`` that resolves to a **loader output** —
    ``static_param``, ``sheet_read``, ``bigquery_query``, etc.

They MAY NOT reference a reasoning-step output. The validator
rejects ``args.channel = "{some_reasoning_step.channel_id}"`` at
freeze. Why: a reasoning step is the LLM. Letting the model pick
the delivery target at fire time means the same authoring intent
could post to a different channel on a different fire — exactly
the drift the contract architecture was built to prevent. Loaders
are deterministic; the LLM is not.

### Session-prefix block for channel / user_id args

``sl_<id>`` is the internal ADK session id ``SlackAdapter.make_session_id``
produces; ``tg_<id>`` is the Telegram analogue. Neither is a valid
external destination — they are bot-internal conventions for keying
ADK sessions. Bezos repeatedly froze contracts with
``channel="sl_<...>"`` (2026-05-13 ``linux_mastery_30_days_v2`` v3
→ v5 → v6), Slack returned ``channel_not_found``, and emit failed.

Block at TWO layers:

  * **Author time** (``validate_adapter_arg_shapes``): freeze
    refuses a literal ``sl_…`` value in ``slack_post.args.channel``
    or a literal ``sl_…`` in ``telegram_dm.args.user_id``.
    Templated values (``{X.Y}``) are skipped — they're resolved at
    fire time.
  * **Fire time** (adapter body): the adapter raises a documented
    ``RuntimeError`` if the (post-render) channel / user_id still
    starts with ``sl_``. Catches templated values that resolve to
    the prefix at runtime.

### Adapter signature-conformance test

``tests/test_contracts_emit_status_check.py:test_all_emit_adapters_resolve_under_basic_call``
calls every registered emit adapter with minimal valid args + a
mocked downstream tool. Any adapter that passes a kwarg the tool's
signature doesn't accept raises ``TypeError`` at test time instead
of at 20:30 Kyiv on production.

The 2026-05-13 ``linux_mastery_30_days_v2`` v5 ``telegram_dm`` crash
(``telegram_send_dm() got an unexpected keyword argument 'user_id'``)
was exactly this gap: the legacy adapter passed ``user_id=`` to a
tool whose parameter was ``person``, and prior tests stopped at the
adapter's arg-validation gate without ever exercising the wrapping
call. Adding adapters now requires a row in ``minimal_args`` so the
guard keeps growing with the registry.

### Worker defense layers around emit results

The worker now applies three orthogonal checks to every adapter
return value BEFORE recording the emit as successful:

  1. **Coroutine guard**. ``inspect.iscoroutine(result)`` rejects an
     adapter that returned an un-awaited inner coroutine (the
     2026-05-13 ``slack_post`` original sin). The coroutine is
     closed to suppress the GC RuntimeWarning, and a
     ``RuntimeError`` is raised so the worker's ``emit_failed``
     branch fires.
  2. **Status-dict guard**. Any returned dict whose ``status`` is
     in ``{"error", "failed", "not_found", "ambiguous"}`` is
     treated as if the adapter had raised. Backstops adapters that
     forget to raise on tool-side errors.
  3. **Audit enrichment**. Successful emits log ``result_type``
     (e.g. ``"dict"``, ``"NoneType"``, ``"coroutine"`` —
     should never appear given check 1) and a 200-char
     ``result_repr``. Lets operators grep historical audit JSONL
     for phantom successes (None returns, surprising shapes)
     without needing to re-fire the contract.

### Adapter status discipline (2026-05-13 silent no-op)

`linux_mastery_30_days_v2` fired at 20:10 Kyiv on 2026-05-13 with
``emit_count: 1`` and ``ok: true`` in the audit — but nothing landed
in Slack. Root cause was two compounding bugs:

  1. The ``slack_post`` adapter did ``return slack_post_message(...)``
     without ``await``. ``slack_post_message`` is async, so the
     return value was the un-awaited inner coroutine. The worker
     awaited the OUTER adapter coroutine, got the un-awaited inner
     coroutine back, attached it to ``emit_results``, and the
     coroutine was garbage-collected without ever firing the HTTP
     request. Slack received zero traffic.
  2. The worker's emit-success branch wrote ``ok: true`` purely on
     "the adapter coroutine completed without raising". It never
     inspected the returned dict's ``status``. So even an adapter
     that returned ``{"status": "error", "message": "..."}`` was
     recorded as a successful emit.

Two changes pin this class of bug:

  * Every adapter that delegates to an async tool MUST ``await`` it
    AND must raise on ``status != "success"``. ``slack_post`` and
    ``telegram_dm`` were updated; new adapters should follow the
    same pattern. The inner ``RuntimeError`` cause routes through
    the worker's ``emit_failed`` branch and into
    ``_on_failure`` → ``notify_admins``.
  * The worker also inspects the returned dict and treats any
    ``status`` in ``{"error", "failed", "not_found", "ambiguous"}``
    as a raise. Defense-in-depth — catches the same pattern for any
    adapter that forgets the explicit raise.

### Boundary failures (run_contract_fire)

``_on_failure`` only runs if the worker reached its main loop. Three
paths used to raise BEFORE reaching it, each routing through plain
``logger.error/exception`` and nothing else:

  * ``contract_store.load`` fails in ``executor.run_contract_fire`` —
    the on-disk body is missing or its hash drifted.
  * ``worker.execute_contract`` raises ``ContractFireError`` at
    ``worker.py:306`` because the contract isn't ``STRICT``.
  * ``worker.execute_contract`` raises ``ContractFireError`` at
    ``worker.py:316-320`` because the hash on disk no longer matches
    the hash the scheduler stored on the job.

All three now route through ``_alert_boundary_failure`` in
``app/contracts/executor.py``, which spins up a fresh event loop and
calls ``notify_admins`` so the failure is BOTH persisted to
``data/contract_failures.jsonl`` AND DM'd to admins. The disk write
is guaranteed; transport is best-effort.

### Failure alerts (2026-05-13 rewrite)

Worker `_on_failure` routes through `app/contracts/admin_alert.py`,
not the broken `run_emit("telegram_dm", ...)` adapter chain. Why:
the legacy adapter delegated to `telegram_send_dm(person=...)` which
did a **name lookup** against the roster — a numeric `user_id` like
`"330959414"` never matched a name, so `status="not_found"` came
back, the worker didn't inspect the status, and counted the alert
as delivered. Result: `ai_pilot_wed_v3` failed at 18:00 Kyiv on
2026-05-13 and no admin was notified.

The new path has three independent guarantees:

  1. **Disk-first persistence.** Every alert is appended to
     `data/contract_failures.jsonl` BEFORE any transport. A
     transport-broken environment can't hide the event — `tail -f`
     surfaces it.
  2. **Direct Telegram send.** `_send_via_telegram_direct(chat_id,
     text)` POSTs to `api.telegram.org/bot<TOKEN>/sendMessage`
     directly. Only counts as `delivered` on **HTTP 200 + ok=true**.
  3. **Multi-strategy chat_id resolution.** `_resolve_chat_id` tries
     the canonical roster key (`tg_<id>`), the raw form, and finally
     falls back to interpreting a bare numeric id as a chat_id
     (Telegram private chats: chat_id == user_id). The 2026-05-13
     bug specifically triggered the fallback path — admins whose
     roster entry was stale still get the DM.

When 0/N admins receive an alert, a second JSONL line marked
`alert_transport_failed` is written so disk alone explains why no
human saw the alert. Without this line, broken transports cascade
into "looks like nothing failed."

`on_failure.notify` recipients listed on the contract spec are
**additive**: they go through the same direct Telegram path AFTER
the admin set, never replacing it. Spec authors can't accidentally
silence admin alerts by setting `notify=[]`.

### Bot context after a contract fire (audit mirror)

The Slack and Telegram pollers ingest human posts into a
channel-scoped ADK session (``sl_<channel>`` / ``tg_<chat>``). The
pollers DROP bot-message subtype events to avoid mirror loops on
their own output (``slack_poller.py:277-278``). Legacy scheduled
tasks worked around this by calling ``tasks._inject_into_session``
post-delivery. Contracts skipped the workaround entirely, so the
bot had no record of its own scheduled posts and would honestly
answer "I haven't posted anything" when the user asked a follow-up
five minutes after a contract fire.

The worker now calls ``app/contracts/audit_mirror.py:mirror_emit_to_session``
after every successful emit. It:

  * Resolves the emit's target to a session id via the same
    ``make_session_id`` convention the pollers use (``slack_post``
    channel ``C012`` → ``sl_C012``; ``telegram_dm`` user ``330959414``
    → ``tg_330959414``).
  * Appends a model-role event with ``author="contract_runner"`` so
    the bot can distinguish contract-driven turns from human turns
    or its own interactive replies.
  * No-ops cleanly when there's no resolvable target, no runner, no
    pre-existing session, or ADK raises — contract fires must not
    fail because of a mirror miss.

Slack ``#name`` channel references (rather than raw IDs) return
None from the resolver because mapping a name → id needs a Slack
API call, and the bot's interactive ingest path will pick up
follow-ups on that channel anyway. Persistent-store adapters
(``sheet_append``, ``drive_doc_fill``, ``memory_update``) also
return None — they have no chat session to mirror into.

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

## Author identity vs. scheduled-task caller identity

These are **independent surfaces** and must not be conflated when
debugging an auth-shaped failure.

* **Contract emit adapters** resolve the Google/OAuth caller via
  `_contract_author(state)` (`app/contracts/emit.py:257`). The
  author email is set at freeze time + carried in the contract
  body; the worker injects it into `state["__contract__"]["author"]`
  for every fire (`app/contracts/worker.py:327-336`). Adapters that
  need a token (`sheet_append`, `drive_doc_fill`) call
  `_google_token_for_author(_contract_author(state))` —
  author-driven, NOT chat-driven.

* **LLM-driven scheduled tasks** (`run_scheduled_task` /
  `schedule_recurring_task` / `cron_*` jobs) resolve per-user tools
  via `state["user_id"]`. The scheduler injects the chat's session
  id into the durable side-session AND ships the owner identity
  through the caller-tag mechanism: `extract_agent_response(...,
  actual_caller_id=owner_user_id)` prepends a `[__caller_id:X__]`
  marker which the Coordinator-only `state_setter`
  before-agent-callback strips + writes into `state["user_id"]`
  (`app/callbacks/guardrails/core.py:693-700`). Per-user tools like
  `sheets_write` / `drive_list_files` then read `state["user_id"]`
  via `_get_user_email(tool_context)`.

When a scheduled task fails with a Google-not-connected error,
check which path it took:

* `cron_*` / `oneoff_*` / `sched_*` task IDs → LLM-driven path →
  trace the caller-tag chain through `state_setter` → confirm
  `state["user_id"]` was populated correctly.
* `contract:*` task IDs → contract path → trace `_contract_author`
  and the frozen contract body — the author email was set at freeze
  time and survives across all fires of the same contract version.

Mis-attributing an LLM-driven fabrication to a contract auth bug
(or vice versa) sent the 2026-05-20 cron_97f22322 investigation
down the wrong rabbit hole for one full proposal revision. See
`docs/RUNBOOK.md §12`.
