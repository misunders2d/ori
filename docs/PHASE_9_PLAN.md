# Scheduler v2 — Phase 9 plan

Phase 8 shipped 2026-05-15 (tag `v2-phase-8-complete` on
origin at `e8bf26f`). Phase 9 is the **first cutover**:
v2 fires its first end-to-end working schedule via the
`OneOffReminder` template, mounts the authoring toolset
on the `CoordinatorAgent`, and integrates `boot_runtime`
into `run_bot.py`. Per design §12.1 invariant 3 the
v1-path-forbidden gate **lifts at this phase boundary**;
phase 9 is the FIRST phase where files under
`app/contracts/`, `app/tasks.py`,
`app/scheduler_instance.py`, and `data/contracts/` may
be touched. Phase 9 explicitly does not edit them — but
the gate is open from this phase on.

Phase 9 is deliberately scoped to a **single
end-to-end-working surface**: a OneOff reminder
authored through the typed-tool / template pipeline,
fired by the v2 binding, emitted to Slack via a thin
adapter. No ExecutionPlan body, no source loaders, no
compatibility worker, no v1 migration tooling, no
Telegram emit — those land in phases 10 / 11 / 12 +
migration.

**Round-2 revision (2026-05-15)** closes 7 bugs + 1
risk surfaced by codex on the round-1 plan landing
(`5d4948d`). The design §5.2 amendment lands in the
same commit as this revision per reviewer Q1.
`FORBIDDEN_PHASES_1_TO_8` renamed →
`FORBIDDEN_PHASES_PRE_CUTOVER` in `scripts/check_phase_scope.py`
per reviewer Q8.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §5.2 (template-first
  authoring; `TemplateRef.args` shape), §6.7 (boot
  self-test — DEFERRED to a later phase), §11.1
  (compatibility worker — DEFERRED), §11.4 (agent
  guidance / skill migration — MANDATORY for the
  Coordinator mount), §12 step 9, §12.1 invariants 2
  + 3.
- `docs/PHASE_8_PLAN.md` §1.2 (boot self-test deferral
  pointer), §1.3 (OneOff-only freeze + commit — phase
  9 ships the first OneOff schedule end-to-end).

---

## 0. Design-doc amendment (LANDED in this revision)

`TemplateRef(name, version)` on
`app/v2/models/common.py` has no field for template
arguments. The `OneOffReminder` template carries a
`text` argument (the reminder body) that must live on
the ScheduleSpec because the spec is emit-only (no
ExecutionPlan, no source — per design §5.2 "Produces a
ScheduleSpec with no ExecutionPlan").

**Resolution (reviewer Q1 — option A with JSON-only
typing):** `TemplateRef.args: Optional[dict[str,
JsonValue]] = None`. The type is
`pydantic.types.JsonValue` (recursive union of bool /
int / float / str / None / list[JsonValue] / dict[str,
JsonValue]) so non-JSON values cannot leak in and
break the canonical-hash JSON serialisation.

`ScheduleSpec.compute_hash` extended to include
`template.args` in its hashed payload — a body change
re-hashes. Pre-amendment specs (where `args=None`)
round-trip with NO hash drift because `None` was the
field's pre-existing implicit value (the field did not
exist; `args=None` is the same JSON shape as omitting
the field).

The design amendment lands in `docs/CONTRACTS_V2_DESIGN.md`
§5.2 in this revision commit; per phase-7 round-3 /
phase-8 round-1 precedent the commit message carries
`PHASE_OVERRIDE:` naming the amendment.

Per-template arg validation runs inside the template
builder (`OneOffReminder.build`) BEFORE the typed-tool
flow; the spec-layer validator treats `args` as opaque
JSON.

---

## 1. Scope statement

### In scope (phase 9)

1. **TemplateRef.args field + compute_hash extension.**
   Implementation slice of the §0 amendment. Pre-args
   spec round-trips remain hash-stable; post-args
   round-trip pinned with regression tests covering
   `args=None`, `args={}`, populated args, and nested
   JSON shapes.

2. **`OneOffReminder` template** —
   `app/v2/templates/one_off_reminder.py` (NEW
   package). Pure Python builder. Signature:

   ```python
   def build_one_off_reminder(
       *,
       at: datetime,                # tz-aware UTC
       recipient: ChannelRef,       # phase-1 ChannelRef
       text: str,
       owner: UserRef,
       schedule_id: str,
       audit: AuditPolicy = AuditPolicy(),
       failure: FailurePolicy = FailurePolicy(
           on_failure_action=FailureActionType.ALERT_ADMIN
       ),
       clock: Callable[[], datetime],
   ) -> ScheduleSpec
   ```

   Returns a frozen `ScheduleSpec` with:
   - `trigger = OneOffTrigger(at_iso_datetime=at,
     timezone="UTC")`.
   - `delivery = Delivery(target_session_id=
     recipient.external_id,
     fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN)`
     — uses `ChannelRef.external_id` (the field name on
     the existing phase-1 model; round-1 reviewer L98
     fix).
   - `failure` per caller.
   - `template = TemplateRef(name="OneOffReminder",
     version="1", args={"text": text})`.
   - `execution_plan_hash = None` (emit-only).
   - `authored_at` from `clock()` normalised to UTC.
   - `.with_fresh_hash()` populated.

3. **`schedule_create_reminder` typed tool** —
   `app/v2/authoring/templates.py` (NEW module). Async
   ADK tool the LLM calls. The tool surface exposes
   **only the LLM-facing parameters**:

   ```python
   # LLM-facing surface (only these go into the ADK
   # function schema):
   async def schedule_create_reminder(
       at: str,                # ISO 8601 tz-aware
       recipient_channel: str, # Slack channel name or id
       text: str,
   ) -> ToolResponse:
       ...
   ```

   DI happens via a **production wrapper factory**
   (round-1 reviewer L500 fix). The factory accepts
   every DI dependency once at startup and returns a
   closure over them; the closure exposes only the
   LLM-visible parameters. The `AuthoringToolset` calls
   the factory at construction time and registers the
   closure (not the raw function) as the ADK
   `FunctionTool`.

   ```python
   def make_schedule_create_reminder(
       *,
       store: DraftStore,
       handshake_store: HandshakeStore,
       conn_factory: Callable[[], sqlite3.Connection],
       cache_saver: ChannelCacheSaver,
       slack_client: SlackProtocol,
       expected_owner_id: str,
       clock: Callable[[], datetime],
       event_id_factory: Callable[[], str],
       schedule_id_factory: Callable[[], str],
       owner: UserRef,  # the agent's identity carrier
   ) -> Callable[[str, str, str], Awaitable[ToolResponse]]:
       async def schedule_create_reminder(
           at: str,
           recipient_channel: str,
           text: str,
       ) -> ToolResponse:
           # pipeline body
           ...
       schedule_create_reminder.__name__ = (
           "schedule_create_reminder"
       )
       return schedule_create_reminder
   ```

   Pin: the LLM-visible function signature carries
   exactly three parameters (`at`, `recipient_channel`,
   `text`); no DI parameter leaks into the ADK schema.
   The test introspects `FunctionTool` for the
   wrapped function's signature.

   Internal pipeline (each step short-circuits on a
   non-ok `ToolResponse`):

   - parse `at` ISO datetime → UTC-only via
     `datetime.fromisoformat`. Naive datetime →
     `validation_failed(naive_at_datetime)`. Non-ISO →
     `validation_failed(invalid_iso_datetime)`.
   - resolve `recipient_channel` via the phase-6 cache
     resolver (cache-hit fast path; cache-miss →
     `cache_unavailable` shape; channel-not-found →
     `not_found`).
   - `schedule_id = schedule_id_factory()`.
   - `build_one_off_reminder(...)` → spec.
   - Persist the spec as a draft via DraftStore.
   - `schedule_dry_run(draft_id, validate_only, ...)`
     records the handshake.
   - `schedule_freeze(draft_id, ...)` verifies.
   - `schedule_draft_commit(draft_id, ...)` lands the
     schedule + `schedule_created` event; deletes
     draft + handshake on success.
   - Return `ToolResponse.ok(schedule_id, spec)` per
     reviewer Q6.

4. **Slack emit adapter** —
   `app/v2/emit/slack_reminder.py` (NEW package).
   Protocol-typed Slack send. Signature:

   ```python
   async def emit_reminder_to_slack(
       *,
       spec: ScheduleSpec,
       slack_client: SlackProtocol,
       clock: Callable[[], datetime],
   ) -> SlackPostResult
   ```

   - Reads `spec.template.args["text"]`; missing key →
     wrapped to `SlackPostResult(ok=False,
     error="template.args missing 'text'")`.
   - Reads `spec.delivery.target_session_id` (Slack
     channel id).
   - Calls `slack_client.chat_postMessage(channel,
     text)`.
   - Returns a `SlackPostResult` Pydantic carrying
     `ok` / `channel` / `ts` / `error`.
   - Does NOT touch the EventLedger — the worker emit
     branch records `run_succeeded` / `run_failed`
     based on the result.

5. **Worker runtime emit branch** —
   `app/v2/runtime/worker.py` gains a branch in the
   claim → execute pipeline.

   **Schedule-spec fetch (round-1 reviewer L201 fix):**
   `Run` carries `schedule_id` + (optional)
   `execution_plan_hash` only — NOT the spec. The
   branch fetches the spec via
   `get_schedule(conn, run.schedule_id)`:

   - `get_schedule` returns `None` (schedule removed
     between Run insert and claim) → emit
     `run_failed` with reason
     `schedule_not_found_at_claim`; status =
     `failed`. NO emit fires.
   - Spec status is `archived` or `paused` → emit
     `run_failed(schedule_inactive_at_claim)`; status
     = `failed`. (A pending Run for a paused / archived
     schedule should have been cancelled by the
     lifecycle hook; this branch is a defence-in-depth
     pin.)
   - Spec template name has changed since the Run was
     queued (`spec.template.name != "OneOffReminder"`)
     → emit
     `run_failed(schedule_template_changed_at_claim)`.
     The fix path is for the author to delete + re-
     author; phase-9 does not implement re-author.

   When the fetch returns a valid OneOffReminder spec:

   - Dispatch to `emit_reminder_to_slack(spec,
     slack_client, clock)`.
   - On `ok=True`: append `run_succeeded` event +
     transition Run to `succeeded`.
   - On `ok=False`: route per
     `spec.failure.on_failure_action`. Phase 9
     implements `alert_admin` (admin_alert_sent event
     written; phase-10 audit-mirror picks up the
     transport delivery) and `abort_silent` (run_failed
     event only, no admin alert). `retry_later` is
     **out of scope for phase 9** — surface as
     `UnsupportedFailurePolicyError` and fall back to
     `alert_admin` semantics with a warning log.

   Existing branches (no template AND no execution
   plan, or `execution_plan_hash` set) raise
   `UnsupportedSpecError`. Phase 10 / 12 expand the
   branch table.

6. **`expected_owner_id` env default** — phase-7
   round-2 L365 / Q10 deferred this. Add a helper
   `app/v2/runtime/_owner_default.py` that reads
   `V2_AUTHORING_OWNER_ID` from the environment **once
   at import time** and exposes it as a constant.

   Used by the `AuthoringToolset` constructor as
   fallback when the explicit kwarg is `None`:
   - explicit kwarg present → wins.
   - explicit kwarg None + env present → env wins.
   - explicit kwarg None + env missing →
     `RuntimeError` at startup (no silent default).

   Pin: `_owner_default.py` is the documented
   exception to phase-5 rule 10 (only `_defaults.py`
   may read external state at module load). The
   import-hygiene smoke test allows `os.environ` here
   explicitly.

7. **`boot_runtime` integration into `run_bot.py`** —
   currently `boot_runtime` (phase 5) is unmounted;
   phase 9 wires it.

   **Boot ordering (round-1 reviewer L219 fix):** the
   existing `boot_runtime` already starts the binding
   `paused=True` (per
   `app/v2/runtime/boot.py:194-203`). Phase 9 keeps
   that semantic AND adds an explicit unpause step in
   `run_bot.py` AFTER the Slack / Telegram transports
   are online. Sequence:

   1. v1 scheduler boots (existing).
   2. v2 `boot_runtime` runs: recovery → binding start
      (`paused=True`) → OneOff backfill → register
      active schedules → start worker pool. Workers
      claim pending Runs but the binding does not fire
      new wakeups while paused.
   3. Slack / Telegram pollers + clients come online.
   4. v2 binding unpauses (new explicit step). Wakeups
      become active. Overdue OneOff reminders fire.
      Transport-readiness race closed.

   **Jobstore path (round-1 reviewer L229 risk +
   reviewer Q9):** use the existing phase-5 default
   `sqlite:///data/scheduler-v2-jobs.db`. No new path.
   `run_bot.py` does NOT pass `jobstore_url`
   explicitly; the default is the production wiring.

   **Shutdown (round-1 reviewer L478 fix):** store the
   `RuntimeHandle` returned by `boot_runtime`. In the
   shutdown path of `run_bot.py`, call
   `await shutdown_runtime(handle)` BEFORE (or
   alongside) the v1 scheduler shutdown. Pin via a
   test that ensures the handle is stored and a
   shutdown call is wired.

   **Boot self-test (§6.7) DEFERRED** (reviewer Q2
   confirmed). Phase 9 logs a
   `boot_runtime_complete` event after the unpause
   step; the admin-alert self-test gate lands in a
   later phase alongside the audit-mirror work.

8. **`AuthoringToolset` mount on `CoordinatorAgent`** —
   `app/sub_agents/coordinator_agent.py` updated to
   add the toolset to its tool list. **Additive
   (reviewer Q4):** v1 contract tools stay mounted
   alongside v2 per §11.1 deprecation timeline. The
   mount happens in the same commit that updates the
   Coordinator instruction text (§11.4 MANDATORY).

   The toolset construction passes the
   `_owner_default` env value + the slack client +
   the cache saver + the clock + event/schedule id
   factories down to the production wrapper factories
   (round-1 reviewer L500 fix). The
   `AuthoringToolset.get_tools` async method composes
   the LLM-facing `FunctionTool` instances from the
   closures.

9. **Coordinator instruction update** — per §11.4
   MANDATORY:
   - "Scheduling law" clause: every scheduled work
     item is created via a v2 typed tool; templates
     first; CustomFlow only when no template fits AND
     admin approval is in place.
   - References to the v2 template tool name
     (`OneOffReminder`) and the typed-tool path
     (`schedule_draft_start` → `schedule_dry_run` →
     `schedule_freeze` → `schedule_draft_commit`).
   - Deprecation note on v1 contract authoring (tools
     stay mounted for ~60 days but the Coordinator
     should prefer v2).

10. **Coordinator instruction guardrail test** —
    `tests/v2/test_coordinator_instruction_guardrail.py`
    (NEW). Per §11.4 MANDATORY. Parses the
    Coordinator's instruction template + agent module
    and asserts:
    - Does NOT contain `contract_freeze(spec:` /
      `contract_freeze(spec ` patterns (deprecated
      freeform signature).
    - DOES contain references to the v2 template tool
      name (`OneOffReminder`).
    - DOES contain the scheduling-law clause.
    - DOES contain `schedule_dry_run` / `schedule_freeze`
      / `schedule_draft_commit` / `schedule_create_reminder`
      string references.

11. **End-to-end pin** —
    `tests/v2/test_e2e_one_off_reminder.py`. Wires the
    DraftStore + HandshakeStore + DB + a stub Slack
    client + a stub channel cache + the v2 binding
    (synchronous / stubbed wakeup per reviewer Q10,
    not real APScheduler timing) and asserts:
    - `schedule_create_reminder` → ok with
      `schedule_id`.
    - DB row exists; `schedule_created` event present.
    - Advancing the test clock past `at` and invoking
      the wakeup callback synchronously inserts a Run.
    - The worker claims + executes the Run; the stub
      Slack client receives one `chat_postMessage`
      call with the expected channel + text.
    - `run_succeeded` event present in the
      EventLedger.
    - Schedule-fetch failure pins (round-1 reviewer
      L201): seed a Run for a schedule that has been
      deleted between insert and claim → assert
      `run_failed(schedule_not_found_at_claim)`.

### Out of scope (phase 9)

- **Boot self-test (§6.7)** — reviewer Q2 confirmed
  deferral.
- **Compatibility worker (§11.1)** — reviewer Q3
  confirmed deferral. Legacy APScheduler jobs continue
  under their current callbacks unchanged.
- **v1 → v2 migration tool (§11.2).**
- **v1 test removal / legacy authoring deprecation.**
- **Telegram emit** — reviewer Q5 confirmed Slack-only.
  Telegram lands in phase 10.
- **Snoozable reminders.** `snoozable=True` in §5.2 is
  a later feature; phase 9 builds emit text and
  terminates.
- **Source loaders / source snapshots.** Phase 10.
- **ExecutionPlan body authoring.** Phases 10 / 12.
- **`RecurringSeriesFromSource` / `ChannelDigest`
  templates.** Phase 11.
- **`schedule_status` / `schedule_diff` / replay
  observability tools (§9).** Phase 15.
- **`FORBIDDEN_PHASES_PRE_CUTOVER` lift / removal.**
  The constant stays in the guard for the deprecation
  window; phase 9 doesn't widen its allowlist to
  cover v1 paths because phase 9 doesn't edit them.
  The gate remains active for phases 1-8 commits via
  the `if phase >= 9` short-circuit.

---

## 2. New file paths

```
# Templates (NEW package)
app/v2/templates/__init__.py
app/v2/templates/one_off_reminder.py

# Authoring extension
app/v2/authoring/templates.py

# Emit adapter (NEW package)
app/v2/emit/__init__.py
app/v2/emit/slack_reminder.py

# Runtime helper
app/v2/runtime/_owner_default.py

# Tests
tests/v2/test_template_one_off_reminder.py
tests/v2/test_authoring_template_tool.py
tests/v2/test_emit_slack_reminder.py
tests/v2/test_runtime_worker_emit_branch.py
tests/v2/test_runtime_owner_default.py
tests/v2/test_coordinator_instruction_guardrail.py
tests/v2/test_e2e_one_off_reminder.py
```

Existing files touched:
- `app/v2/models/common.py` — `TemplateRef.args` added
  (§0 amendment).
- `app/v2/models/schedule.py` — `compute_hash` extended
  to include `template.args`.
- `app/v2/runtime/worker.py` — emit branch for
  template-only specs with `get_schedule` fetch +
  spec-staleness guards.
- `app/v2/toolsets/authoring.py` — add the production
  wrapper factory for `schedule_create_reminder` +
  18-tool `ToolDescriptor` registration.
- `app/v2/authoring/__init__.py` — re-export
  `make_schedule_create_reminder` (factory) +
  `schedule_create_reminder` name (closure surface
  for tests).
- `app/sub_agents/coordinator_agent.py` — toolset
  mount + instruction update.
- `run_bot.py` (or wherever the bootstrap lives) —
  `boot_runtime` call + handle storage + unpause
  step + `shutdown_runtime` in shutdown path.
- `.v2-current-phase` — already at 9 (round-1 landing).
- `scripts/check_phase_scope.py` —
  `PHASE_ALLOWLIST[9]` (already added in round-1) +
  `FORBIDDEN_PHASES_1_TO_8` →
  `FORBIDDEN_PHASES_PRE_CUTOVER` rename (this
  revision).
- `docs/PHASE_9_PLAN.md` — this file.
- `docs/CONTRACTS_V2_DESIGN.md` — §5.2 amendment
  (LANDED in this revision per reviewer Q1).

---

## 3. Module APIs

(See §1 for caller signatures; this section captures
the internal shapes the slice commits must hit.)

### 3.1 `TemplateRef` (amended)

```python
from pydantic.types import JsonValue

class TemplateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    args: Optional[dict[str, JsonValue]] = None  # NEW
```

- `args=None` for legacy spec round-trips.
- `JsonValue` rejects non-JSON values at validation
  time (round-1 reviewer L48 fix). A `datetime` /
  `set` / custom class in the dict raises
  `pydantic.ValidationError`; tests pin this.
- `args` participates in
  `ScheduleSpec.compute_hash` (same JSON-serialised
  path as the other fields). Regression test pins:
  - Same args → same hash.
  - Different args → different hash.
  - `args=None` matches the pre-amendment shape (no
    drift on existing on-disk specs).

### 3.2 `OneOffReminder` template

`app/v2/templates/one_off_reminder.py`:

- `TEMPLATE_NAME = "OneOffReminder"`,
  `TEMPLATE_VERSION = "1"`.
- `class OneOffReminderArgs(BaseModel)` — typed
  Pydantic for the args dict. Fields:
  - `text: str` with `min_length=1, max_length=4000`.
  Pin oversize.
- `build_one_off_reminder(...)` per §1.2 signature.
  Uses `recipient.external_id` for the delivery target
  (round-1 reviewer L98 fix).
- Module hygiene pinned: no `datetime.now` /
  `uuid.uuid4` — clock + factories DI'd (phase-5 rule
  10).

### 3.3 `schedule_create_reminder` production wrapper

`app/v2/authoring/templates.py`:

- `make_schedule_create_reminder(*, store, ..., owner)
  → closure` per §1.3.
- Closure exposes ONLY `(at, recipient_channel, text)`.
- Closure pipeline:
  1. Parse `at` → UTC datetime or `validation_failed(
     naive_at_datetime | invalid_iso_datetime)`.
  2. Resolve `recipient_channel` via phase-6 cache
     resolver → `ChannelRef`.
  3. `schedule_id = schedule_id_factory()`.
  4. `build_one_off_reminder(...)` → spec.
  5. Write the spec to DraftStore.
  6. Call `schedule_dry_run` / `schedule_freeze` /
     `schedule_draft_commit` in sequence; forward any
     non-ok `ToolResponse`.
  7. Return `ok(schedule_id=spec.id,
     spec=spec.model_dump(mode="json"))`.

Failure-shape table:

| Step | Failure → ToolResponse code |
|---|---|
| parse `at` | `validation_failed(naive_at_datetime)` or `validation_failed(invalid_iso_datetime)` |
| resolve recipient | `cache_unavailable` per phase-6 / `not_found` for unknown channel |
| build template | `validation_failed(<template-specific code>)` |
| dry_run / freeze / commit | forwarded codes per phase-8 plan |

### 3.4 Slack emit adapter

`app/v2/emit/slack_reminder.py`:

- `class SlackPostResult(BaseModel)` — `ok: bool`,
  `channel: str`, `ts: Optional[str]`,
  `error: Optional[str]`.
- `SlackProtocol` — minimal protocol type carrying
  `chat_postMessage(channel: str, text: str) ->
  Awaitable[dict]`. Tests pass a stub.
- No imports of `slack_sdk` at module load
  (protocol-typed DI; phase-6 carry-forward).
- Errors caught + wrapped to `SlackPostResult(ok=False,
  error=str(exc))`. Worker emit branch surfaces
  per-FailurePolicy.

### 3.5 Worker emit branch

`app/v2/runtime/worker.py`: new branch in the claim →
execute pipeline.

```python
# Pseudocode for the branch (real implementation in
# slice 5):
schedule = get_schedule(conn, run.schedule_id)
if schedule is None:
    _emit_run_failed(
        conn,
        run,
        reason="schedule_not_found_at_claim",
        ...,
    )
    return
if schedule.status in (ScheduleStatus.ARCHIVED,
                       ScheduleStatus.PAUSED):
    _emit_run_failed(
        conn,
        run,
        reason="schedule_inactive_at_claim",
        ...,
    )
    return
template_name = (
    schedule.template.name
    if schedule.template is not None
    else None
)
if template_name != "OneOffReminder":
    _emit_run_failed(
        conn,
        run,
        reason="schedule_template_changed_at_claim",
        ...,
    )
    return
# OneOffReminder branch: emit + record.
result = await emit_reminder_to_slack(
    spec=schedule,
    slack_client=...,
    clock=...,
)
if result.ok:
    _emit_run_succeeded(conn, run, ...)
else:
    _route_failure_policy(conn, run, schedule, result, ...)
```

### 3.6 `_owner_default`

`app/v2/runtime/_owner_default.py`:

- Reads `V2_AUTHORING_OWNER_ID` env var ONCE at import
  time.
- Exposes `DEFAULT_AUTHORING_OWNER_ID: Optional[str]`.
- `AuthoringToolset.__init__` becomes: if explicit
  `expected_owner_id` is `None` → use the default. If
  both are `None` at startup → raise `RuntimeError`.
- Pin: env populated → default available; env missing
  → default is `None`; both-None → constructor raises.

### 3.7 `boot_runtime` + `shutdown_runtime` integration

`run_bot.py` (or `app/agent.py`):

```python
# Pseudocode (slice 7):
async def main() -> None:
    # 1. v1 scheduler boots (existing).
    ...
    # 2. v2 boot_runtime — binding starts PAUSED.
    v2_handle = await boot_runtime(
        conn_factory=...,
    )
    try:
        # 3. Slack / Telegram transports come online.
        await start_pollers()
        # 4. Unpause v2 binding now that transports
        #    are ready (round-1 reviewer L219 fix).
        v2_handle.binding.resume()
        _logger.info("runtime.boot.unpaused")
        # 5. Serve.
        await run_forever()
    finally:
        # 6. Shutdown v2 BEFORE v1 (round-1 reviewer
        #    L478 fix).
        await shutdown_runtime(v2_handle)
        await stop_v1_scheduler()
```

- Jobstore path: phase-5 default
  `sqlite:///data/scheduler-v2-jobs.db` (round-1
  reviewer Q9). NOT explicitly passed.
- Boot self-test (§6.7) deferred; phase 9 logs a
  `boot_runtime_complete` event after step 4.

### 3.8 Coordinator mount + instruction

`app/sub_agents/coordinator_agent.py`:

- Imports + instantiates the production wrapper
  factories with module-level DI singletons (clock,
  slack client, channel cache saver, conn factory,
  event / schedule id factories). The
  `AuthoringToolset` constructor passes these down so
  the closures register on `get_tools`.
- The toolset is added to the agent's `tools=[…]`
  list **alongside** the existing v1 contract tools
  (additive per reviewer Q4).
- Instruction text gains the §11.4 scheduling-law
  clause and v2 tool references.

### 3.9 End-to-end test

`tests/v2/test_e2e_one_off_reminder.py`:

- Uses `tmp_path` for the v2 DB; runs migrations.
- Builds a Slack channel cache fixture covering the
  test channel.
- Stub Slack client records `chat_postMessage` calls.
- Wraps the test in a freezeable clock so `at` can
  be advanced.
- Pins the full sequence: tool call → DB row → wakeup
  callback invoked synchronously (NOT real
  APScheduler timing per reviewer Q10) → Run claim →
  emit → `run_succeeded`.
- Schedule-fetch failure pins (L201): delete the
  schedule between Run insert and claim; assert
  `schedule_not_found_at_claim`.

---

## 4. Slice ordering + commit cadence

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan) | `docs/PHASE_9_PLAN.md` round-2 + `docs/CONTRACTS_V2_DESIGN.md` §5.2 amend + `FORBIDDEN_PHASES_PRE_CUTOVER` rename | — |
| 1 | `TemplateRef.args` field + `compute_hash` extension | `test_models_common.py` update + new args round-trip pins |
| 2 | `OneOffReminder` template module + args model | `test_template_one_off_reminder.py` |
| 3 | `schedule_create_reminder` production wrapper factory + toolset extension (18 tools) | `test_authoring_template_tool.py` + `test_authoring_toolset.py` count bump + DI-leak pin |
| 4 | Slack emit adapter + `SlackPostResult` | `test_emit_slack_reminder.py` |
| 5 | Worker emit branch (with `get_schedule` fetch + staleness guards) | `test_runtime_worker_emit_branch.py` |
| 6 | `_owner_default` + AuthoringToolset constructor wiring | `test_runtime_owner_default.py` + existing toolset constructor pins update |
| 7 | `boot_runtime` integration into `run_bot.py` (paused-until-transports + shutdown) | existing `test_runtime_boot.py` extended; smoke pin for unpause + shutdown_runtime |
| 8 | `CoordinatorAgent` mount + instruction update + guardrail test | `test_coordinator_instruction_guardrail.py` |
| 9 | End-to-end pin | `test_e2e_one_off_reminder.py` |
| closeout | acceptance walk + tag `v2-phase-9-complete` (gated on codex pass) | — |

10 slices. Reviewer may bundle 1+2, 4+5, or 7+8 during
slice review.

---

## 5. Test inventory

### 5.1 `test_template_one_off_reminder.py`

- `build_one_off_reminder` happy path: returns a
  `ScheduleSpec` with `template.name == "OneOffReminder"`,
  `template.version == "1"`,
  `template.args == {"text": ...}`,
  `execution_plan_hash is None`,
  `trigger.type == "one_off"`,
  `.hash` populated.
- Delivery target equals `recipient.external_id`
  (L98 pin).
- Naive `at` → `ValueError`.
- Empty `text` → Pydantic rejects (`min_length=1`).
- Oversize `text` (4001 chars) → Pydantic rejects.
- AST pin: no `datetime.now` / `uuid.uuid4`.

### 5.2 `test_authoring_template_tool.py`

- Happy path: closure happy path returns
  `ok(schedule_id, spec)`; DB row + `schedule_created`
  event present.
- Naive `at` string → `validation_failed(
  naive_at_datetime)`.
- Invalid ISO → `validation_failed(invalid_iso_
  datetime)`.
- Channel cache miss + network down →
  `cache_unavailable`.
- Channel cache miss + network up + channel not
  found → `not_found`.
- One representative dry-run / freeze / commit
  forwarded failure (e.g. body hash drift injection).
- **DI-leak pin (L500 fix):** introspect the
  `FunctionTool` wrapping the closure; assert the
  signature carries exactly `(at, recipient_channel,
  text)` and NO DI parameter names (`store`, `conn`,
  `clock`, etc.).
- Tool registered on the AuthoringToolset under the
  name `schedule_create_reminder`; ToolDescriptor
  tags = `{DB_WRITE, FILESYSTEM_WRITE, READ_EXTERNAL,
  USES_OAUTH}` (DB + draft files + Slack lookup +
  OAuth via the cache resolver).

### 5.3 `test_emit_slack_reminder.py`

- Happy path: stub client receives one
  `chat_postMessage(channel, text)`; result is
  `SlackPostResult(ok=True, channel=..., ts=...)`.
- Stub raises → `SlackPostResult(ok=False,
  error=...)`.
- Missing `text` in `template.args` →
  `SlackPostResult(ok=False, error="template.args
  missing 'text'")`.
- AST pin: no `slack_sdk` import at module load.

### 5.4 `test_runtime_worker_emit_branch.py`

Schedule-fetch + staleness pins (L201):
- Run with deleted schedule → emit
  `run_failed(schedule_not_found_at_claim)`; no
  Slack call.
- Run with archived schedule → emit
  `run_failed(schedule_inactive_at_claim)`; no Slack
  call.
- Run with paused schedule → same as archived.
- Run with template-name drift (spec mutated to a
  different template after Run insert) → emit
  `run_failed(schedule_template_changed_at_claim)`.

OneOffReminder happy + failure-policy pins:
- Happy: emit fires → `run_succeeded` event present;
  `Run.status == succeeded`.
- Stub emit returns `ok=False` + `alert_admin` →
  emit_failed + admin_alert_sent events present.
- Stub emit returns `ok=False` + `abort_silent` →
  run_failed event present, no admin alert.
- `retry_later` policy → `UnsupportedFailurePolicy`
  warning + falls back to `alert_admin` semantics.

UnsupportedSpec branches:
- Run with no template AND no plan →
  `UnsupportedSpecError` (phase 10 / 12 implements
  CustomFlow).
- Run with `execution_plan_hash` set →
  `UnsupportedSpecError`.

### 5.5 `test_runtime_owner_default.py`

- env populated → default returned; toolset
  constructor uses it as fallback.
- env missing → default is `None`; toolset constructor
  raises `RuntimeError` when explicit kwarg is also
  `None`.
- Explicit kwarg wins over env value.

### 5.6 `test_coordinator_instruction_guardrail.py`

- Parses the resolved Coordinator instruction text.
- Asserts: no `contract_freeze(spec:` /
  `contract_freeze(spec ` substrings.
- Asserts: `OneOffReminder`, `schedule_dry_run`,
  `schedule_freeze`, `schedule_draft_commit`,
  `schedule_create_reminder` all referenced.
- Asserts: scheduling-law clause present (exact
  string fragment pinned).

### 5.7 `test_e2e_one_off_reminder.py`

(See §3.9.) Full happy-path sequence pinned PLUS
the L201 schedule-fetch failure pin.

### 5.8 Carry-forward updates

- `test_authoring_toolset.py` — bump expected tool
  count 17 → 18; add `schedule_create_reminder` to
  `_EXPECTED_TAG_MATRIX`.
- `test_models_common.py` (or wherever TemplateRef
  round-trips are pinned) — add args round-trip
  cases.
- `test_validation.py` — args field is opaque to
  `validate_schedule_spec`; pin that it surfaces no
  issues on a OneOffReminder spec.
- `test_runtime_boot.py` — `boot_runtime` mounted but
  self-test not gated; pin via explicit assertion on
  the boot sequence; pin paused-until-unpause +
  shutdown semantics via stubs.

---

## 6. CI guard checks

`scripts/check_phase_scope.py` after this revision:

```python
PHASE_ALLOWLIST[9] = {
    # v2 scope (carried)
    "app/v2/",
    "tests/v2/",
    "scripts/check_phase_scope.py",
    "scripts/install_hooks.py",
    ".githooks/v2_phase_guard.sh",
    ".githooks/pre-commit",
    ".github/workflows/v2_phase_guard.yml",
    ".v2-current-phase",
    "docs/PHASE_9_PLAN.md",
    "docs/CONTRACTS_V2_DESIGN.md",
    ".docs_read_marker",
    # phase-9 cutover surface
    "app/sub_agents/coordinator_agent.py",
    "run_bot.py",
    "app/agent.py",
}
```

**Forbidden-paths constant rename (reviewer Q8):**
`FORBIDDEN_PHASES_1_TO_8` →
`FORBIDDEN_PHASES_PRE_CUTOVER`. The runtime check
`if phase >= 9: return False` short-circuits at
phase 9, so the rename is purely a clarity edit.
The error-report string is updated to "forbidden in
phases 1-8" to match the runtime semantics.

Cross-cutting smoke checks (carry-forward):

- Phase-9 NEW modules import no I/O libs at module
  load. Slack client is protocol-typed DI per
  phase-6 carry-forward.
- Phase-9 NEW modules do NOT import
  `app.v2.runtime._defaults` at module load. Clock +
  id factories stay DI.
- `_defaults.py` remains the ONLY runtime / authoring
  module whose smoke test asserts `uuid` /
  `datetime.now` imports.
- **Documented exception:** `_owner_default.py` IS
  allowed to read `os.environ` at module load. The
  import-hygiene smoke test allowlists this single
  module.

---

## 7. Acceptance criteria for `v2-phase-9-complete`

1. Branch ahead of `v2-phase-8-complete` by N small
   commits, each scoped to one slice in §4.
2. `TemplateRef.args: Optional[dict[str, JsonValue]]`
   round-trips through DraftStore + ScheduleStore +
   `ScheduleSpec.compute_hash`; pre-args specs
   (args=None) still round-trip unchanged; non-JSON
   values rejected at validation.
3. `build_one_off_reminder` returns a frozen spec
   with the documented shape; naive `at` rejected;
   empty / oversize text rejected;
   `recipient.external_id` is the delivery target
   (L98 pin).
4. `schedule_create_reminder` closure happy-path
   lands a schedule + `schedule_created` event;
   failure shapes documented in §5.2 all pinned; DI
   parameter names absent from the FunctionTool
   signature (L500 pin).
5. Slack emit adapter sends `chat_postMessage` with
   the documented (channel, text) and surfaces a
   typed `SlackPostResult`.
6. Worker emit branch fetches the spec via
   `get_schedule(conn, run.schedule_id)` (L201 fix);
   handles `None` / archived / paused / template-name
   drift each with a documented `run_failed` reason
   code; happy path writes `run_succeeded`;
   FailurePolicy routing pinned for `alert_admin` +
   `abort_silent`.
7. `_owner_default` env fallback works; explicit
   kwarg wins; both-None raises `RuntimeError` at
   startup.
8. `boot_runtime` is called from `run_bot.py` after
   v1 boot; binding starts PAUSED (L219 fix) and
   unpauses AFTER transports come online; lifecycle
   hooks wired so phase-7 lifecycle tools can fire
   them. Jobstore path is the phase-5 default
   `sqlite:///data/scheduler-v2-jobs.db` (L229 risk
   fix / Q9). Shutdown path calls `shutdown_runtime`
   (L478 fix).
9. `AuthoringToolset` mounted on `CoordinatorAgent`;
   Coordinator instruction text gains the §11.4
   scheduling-law clause; guardrail test green.
10. End-to-end test: `schedule_create_reminder` →
    DB + event → wakeup fires (synchronous stub per
    Q10) → worker claims + emits → Slack stub
    receives one call → `run_succeeded` event.
    Schedule-fetch failure pin present.
11. Phase guard `--diff v2-phase-8-complete` clean.
12. v1 paths NOT touched in any phase-9 commit (out
    of scope per §1; the v1-path gate lifts but is
    not exercised).
13. No `datetime.now()` / `uuid.uuid4()` outside
    `_defaults.py`. AST pin on every phase-9 NEW
    module. `_owner_default.py` is the documented
    env-read exception.
14. Full v2 test suite passes (existing 1696 +
    phase-9 adds); no regressions.
15. Coordinator instruction guardrail test asserts
    the v1 freeform signature is absent + v2 tool
    names are present.
16. Annotated git tag `v2-phase-9-complete` created
    and pushed (workflow pre-approved per phase 5–8
    pattern).

---

## 8. Tag annotation

```
v2 phase 9 complete

First v2 end-to-end cutover. OneOffReminder template +
schedule_create_reminder typed tool + Slack emit adapter
+ worker emit branch + boot_runtime integration into
run_bot.py + AuthoringToolset mount on CoordinatorAgent.

A OneOff reminder authored via the v2 template tool now
fires end-to-end through the new wakeup → claim → emit
path; the legacy v1 contract scheduler remains
untouched and continues running pre-existing schedules
under its existing callbacks per §11.1.

TemplateRef gains an optional args dict (JsonValue-typed)
so emit-only templates can carry per-instance payload
(reminder text) without an ExecutionPlan.
ScheduleSpec.compute_hash includes args so a body change
re-hashes; pre-args specs round-trip unchanged via
args=None.

expected_owner_id picks up V2_AUTHORING_OWNER_ID env
fallback (deferred from phase 7 round-2 L365); explicit
kwarg still wins; both-None raises a startup error.

run_bot.py boots v2 PAUSED, brings transports online,
THEN unpauses the v2 binding so overdue OneOff reminders
can't fire before Slack / Telegram are ready (round-1
reviewer L219 fix). Shutdown path calls
shutdown_runtime(handle) before v1 scheduler shutdown
(round-1 reviewer L478 fix). Jobstore stays at the
phase-5 default sqlite:///data/scheduler-v2-jobs.db
(round-1 reviewer Q9).

Worker fetches ScheduleSpec via get_schedule(conn,
run.schedule_id) before emitting; missing / archived /
paused / template-name-drift each surface a documented
run_failed reason (round-1 reviewer L201 fix).

The schedule_create_reminder ADK tool exposes ONLY
(at, recipient_channel, text) to the LLM via a
production wrapper factory (round-1 reviewer L500 fix);
DI parameters never leak into the function schema.

Coordinator instruction gains the §11.4 scheduling-law
clause + v2 tool references. v1 authoring tools stay
mounted alongside v2 per the deprecation timeline
(reviewer Q4 additive).

NOT shipped (deferred): boot self-test (§6.7),
compatibility worker (§11.1), v1 → v2 contract
migration tool, Telegram emit, snoozable reminders,
source loaders, ExecutionPlan authoring.

Design: docs/CONTRACTS_V2_DESIGN.md §5.2, §11.4,
        §12 step 9
Plan:   docs/PHASE_9_PLAN.md
```

---

## 9. Open questions

### 9.1 Closed in this revision (round-1 + round-2)

1. ~~`TemplateRef.args` storage shape.~~ **CLOSED**
   (round-1 reviewer L48 + Q1): option (a) field on
   `TemplateRef` with `Optional[dict[str, JsonValue]]`;
   design §5.2 amendment lands in this revision
   commit.
2. ~~Boot self-test (§6.7) scope.~~ **CLOSED**
   (round-1 reviewer Q2): defer.
3. ~~Compatibility worker (§11.1) scope.~~ **CLOSED**
   (round-1 reviewer Q3): defer.
4. ~~AuthoringToolset mount additive vs replacement.~~
   **CLOSED** (round-1 reviewer Q4): additive.
5. ~~Telegram emit scope.~~ **CLOSED** (round-1
   reviewer Q5): Slack-only.
6. ~~`schedule_create_reminder` return shape.~~
   **CLOSED** (round-1 reviewer Q6): wrapped
   `ok(schedule_id, spec)`.
7. ~~`PHASE_ALLOWLIST[9]` scope.~~ **CLOSED** (round-1
   reviewer Q7): tight; don't add `app/contracts/*`
   unless actually touched.
8. ~~`FORBIDDEN_PHASES_1_TO_8` rename.~~ **CLOSED**
   (round-1 reviewer Q8): renamed
   `FORBIDDEN_PHASES_PRE_CUTOVER` in this revision.
9. ~~v2 jobstore location.~~ **CLOSED** (round-1
   reviewer Q9): keep phase-5 default
   `sqlite:///data/scheduler-v2-jobs.db`.
10. ~~End-to-end test timing model.~~ **CLOSED**
    (round-1 reviewer Q10): synchronous / stubbed
    default; real-APScheduler smoke deferred.

Round-1 bugs closed in this revision:

- L48 → JsonValue typing for `args`.
- L68 → design amendment lands in this commit.
- L98 → builder uses `ChannelRef.external_id`.
- L201 → worker fetches via `get_schedule` + 4
  staleness reason codes.
- L219 → boot keeps `paused=True`; explicit unpause
  step after transports ready.
- L229 → jobstore path stays at phase-5 default.
- L478 → `shutdown_runtime` wired into `run_bot.py`
  shutdown path.
- L500 → production wrapper factories +
  DI-leak-pin test.

### 9.2 Still open

None. Round-2 will populate here if reviewer surfaces
new gaps on this revision.

---

## 10. Hard rules (carried forward)

Same as phase 8 plan §10 with phase-9 additions:

1. No push without explicit reviewer / Sergey
   approval.
2. No edits to phase-1 through phase-8 plan docs
   without `PHASE_OVERRIDE:` in the commit message.
   The §0 design amendment for `TemplateRef.args`
   carries `PHASE_OVERRIDE:` on this revision commit.
3. No time estimates.
4. Pause after each commit for reviewer.
5. `uv run python …` always.
6. Pre-commit hook needs `.docs_read_marker` —
   `echo "yes" | uv run python scripts/check_docs_read.py`.
7. Commit messages end with
   `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
8. Leave `app/tools/youtube.py` dirty/uncommitted
   unless reviewer flags otherwise; same for the
   three diag scripts at the repo root.
9. **v1 paths may be touched starting phase 9** per
   §12.1 invariant 3 (boundary lift). Phase 9
   explicitly does NOT touch them; phase 10+ will.
10. Every runtime / authoring helper takes injected
    clock + id factories; `_defaults.py` is the ONLY
    module that wires them to wall clock + uuid4.
    `_owner_default.py` is the documented exception
    that reads env once at import time.
11. **AuthoringToolset mount is ADDITIVE** in phase 9
    per §11.1 deprecation timeline; v1 contract tools
    stay mounted on the Coordinator alongside v2
    until the ~60-day sunset.
12. **v2 binding starts PAUSED**; phase 9 unpauses
    only AFTER transports are online (round-1 L219
    fix). Future phases that boot v2 in different
    contexts must respect this ordering.
13. **DI parameters do not appear in LLM-facing tool
    schemas.** Use the production wrapper factory
    pattern (closure over DI; expose only LLM
    parameters). Tests pin via FunctionTool signature
    introspection.
