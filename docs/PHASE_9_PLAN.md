# Scheduler v2 — Phase 9 plan

Phase 8 shipped 2026-05-15 (tag `v2-phase-8-complete` on
origin at `e8bf26f`). Phase 9 is the **first cutover**:
v2 fires its first end-to-end working schedule via the
`OneOffReminder` template, mounts the authoring toolset on
the `CoordinatorAgent`, and integrates `boot_runtime` into
`run_bot.py`. Per design §12.1 invariant 3 the
v1-path-forbidden gate **lifts at this phase boundary**;
phase 9 is the FIRST phase where files under
`app/contracts/`, `app/tasks.py`,
`app/scheduler_instance.py`, and `data/contracts/` may be
touched.

Phase 9 is deliberately scoped to a **single
end-to-end-working surface**: a OneOff reminder authored
through the typed-tool / template pipeline, fired by the
v2 binding, emitted to Slack via a thin adapter. No
ExecutionPlan body, no source loaders, no compatibility
worker, no v1 migration tooling, no Telegram emit — those
land in phases 10 / 11 / 12 + migration.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §5.2 (template-first
  authoring; `OneOffReminder(at, recipient, text,
  snoozable=False)`), §6.7 (boot self-test — deferred to a
  later phase), §11.1 (compatibility worker — deferred),
  §11.4 (agent guidance / skill migration — MANDATORY for
  the Coordinator mount), §12 step 9, §12.1 invariants 2 +
  3.
- `docs/PHASE_8_PLAN.md` §1.2 (boot self-test deferral
  pointer), §1.3 (OneOff-only freeze + commit — phase 9
  ships the first OneOff schedule end-to-end).

---

## 0. Design-doc amendment (REQUIRED — flag for round-1)

`TemplateRef(name, version)` on `app/v2/models/common.py`
has **no field for template arguments**. The
`OneOffReminder` template carries a `text` argument (the
reminder body) that must live on the ScheduleSpec because
the spec is emit-only (no ExecutionPlan, no source — per
design §5.2 "Produces a ScheduleSpec with no
ExecutionPlan"). The reminder body has nowhere to land
without amending one of:

a. `TemplateRef.args: Optional[dict[str, Any]] = None` —
   structured args dict per-template; the schema is the
   template's responsibility. Round-trips through
   `ScheduleSpec.compute_hash` so a body change re-hashes.
b. New top-level `ScheduleSpec.static_emit_body:
   Optional[StaticEmitBody] = None` with a typed
   sub-model. More invasive — changes the spec shape.
c. A minimal synthetic `ExecutionPlan` body containing
   the literal text. Contradicts §5.2 ("no ExecutionPlan")
   and §12 step 9 ("emit-only path").

**Proposed (round-1):** option (a). Smallest delta;
keeps the ScheduleSpec shape stable; lets future templates
(`RecurringSeriesFromSource`, `ChannelDigest`) reuse the
same field without per-template fields proliferating.
`TemplateRef.args` is opaque to the validator at the spec
layer; per-template validation runs inside the template
builder (`OneOffReminder.build(at, recipient, text, …)`)
BEFORE the typed-tool flow.

The amendment lands in slice 0 (this plan + design §5.2
edit) as a `PHASE_OVERRIDE:` commit message line on the
plan landing commit (per phase 7 round-3 / phase 8 round-1
precedent for in-flight design amendments). The
amendment commit edits `docs/CONTRACTS_V2_DESIGN.md` §5.2
to name the field, plus adds `template.args` round-trip
to the canonical `ScheduleSpec.compute_hash` (this is a
behaviour change — pin via a regression test).

If round-1 reviewer disagrees, fall back to (b) and amend
this §0 plus the slice plan accordingly.

---

## 1. Scope statement

### In scope (phase 9)

1. **Design amendment** — add `TemplateRef.args` per §0;
   updates `ScheduleSpec.compute_hash` so the args
   participate in the canonical hash.

2. **`OneOffReminder` template** —
   `app/v2/templates/one_off_reminder.py` (NEW package).
   Pure Python builder. Signature:

   ```python
   def build_one_off_reminder(
       *,
       at: datetime,          # tz-aware UTC; OneOffTrigger.at_iso_datetime
       recipient: ChannelRef, # Delivery target (Slack channel / DM)
       text: str,             # reminder body
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
   - `trigger = OneOffTrigger(at_iso_datetime=at, timezone="UTC")`.
   - `delivery = Delivery(target_session_id=recipient.id,
     fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN)`.
   - `failure` per caller.
   - `template = TemplateRef(name="OneOffReminder",
     version="1", args={"text": text})`.
   - `execution_plan_hash = None` (emit-only).
   - `authored_at` from `clock()` normalised to UTC.
   - `.with_fresh_hash()` populated.

3. **`schedule_create_reminder` typed tool** —
   `app/v2/authoring/templates.py` (NEW module). Async
   ADK tool the LLM calls. Signature (caller-side):

   ```python
   async def schedule_create_reminder(
       at: str,                # ISO 8601 tz-aware
       recipient_channel: str, # Slack channel name or id
       text: str,
       *,
       session_id: str,
       owner_platform: str,
       owner_user_id: str,
       store: DraftStore,
       handshake_store: HandshakeStore,
       conn: sqlite3.Connection,
       cache_saver: ChannelCacheSaver,
       slack_client: SlackProtocol,
       expected_owner_id: str,
       clock: Callable[[], datetime],
       event_id_factory: Callable[[], str],
       schedule_id_factory: Callable[[], str],
   ) -> ToolResponse
   ```

   Internal pipeline (each step short-circuits on a
   non-ok `ToolResponse`):

   - parse `at` ISO datetime → UTC-only via
     `datetime.fromisoformat` + tz validator. Naive
     datetime → `validation_failed(naive_at_datetime)`.
   - resolve `recipient_channel` via the phase-6 cache
     resolver (cache-hit fast path; cache-miss triggers
     a `cache_unavailable` shape).
   - `schedule_id = schedule_id_factory()`.
   - call `build_one_off_reminder(...)` → spec.
   - persist the spec as a draft (write the
     ScheduleSpecDraft superset to the DraftStore).
   - call `schedule_dry_run(draft_id, validate_only,
     ...)` — records the handshake.
   - call `schedule_freeze(draft_id, ...)` — gates on the
     OneOff trigger + handshake freshness.
   - call `schedule_draft_commit(draft_id, ...)` — atomic
     insert + `schedule_created` event; deletes draft +
     handshake on success.
   - return `ToolResponse.ok(schedule_id, spec)` on
     success; surface intermediate `ToolResponse` codes
     verbatim on any failure.

   The tool is the **agent-facing primary surface** for
   phase-9 OneOff reminders. The lower-level typed-tool
   path (`schedule_draft_start` → setters → compile → …)
   stays for CustomFlow / future templates.

4. **Slack emit adapter** —
   `app/v2/emit/slack_reminder.py` (NEW package). Pure
   protocol-typed Slack send. Signature:

   ```python
   async def emit_reminder_to_slack(
       *,
       spec: ScheduleSpec,
       slack_client: SlackProtocol,
       clock: Callable[[], datetime],
   ) -> SlackPostResult
   ```

   - Reads `spec.template.args["text"]` (KeyError → wrap
     to a structured emit-failure result).
   - Reads `spec.delivery.target_session_id` (Slack
     channel id).
   - Calls `slack_client.chat_postMessage(channel,
     text)`.
   - Returns a `SlackPostResult` Pydantic carrying
     ts / channel / ok.
   - Does NOT touch the EventLedger — the worker emit
     branch (slice 5) records `run_succeeded` /
     `run_failed` based on the result.

5. **Worker runtime emit branch** —
   `app/v2/runtime/worker.py` gains a branch when
   `Run.spec.execution_plan_hash is None AND
   spec.template.name == "OneOffReminder"`: claim → emit
   via the Slack adapter → record `run_succeeded` (or
   `run_failed` per `FailurePolicy`) atomically.
   ExecutionPlan-driven branches stay unimplemented in
   phase 9 (phase 10 / 12).

6. **`expected_owner_id` env default** — phase-7 round-2
   L365 / Q10 deferred this. Add a helper
   `app/v2/runtime/_owner_default.py` that reads
   `V2_AUTHORING_OWNER_ID` from the environment ONCE at
   import time and exposes it as a constant. Used by the
   `AuthoringToolset` constructor default IF the explicit
   kwarg is not supplied. Tests pin: explicit kwarg wins;
   env value used as fallback; missing env raises a
   clear startup error (no silent default).

7. **`boot_runtime` integration into `run_bot.py`** —
   currently `boot_runtime` (phase 5 slice 7a) is
   unmounted; phase 9 wires it. Touchpoints:

   - `run_bot.py` (or `app/agent.py` if the bootstrap
     lives there) — import + call `boot_runtime` AFTER
     the v1 scheduler boots, BEFORE the Slack / Telegram
     poller starts handling messages. The v2 binding
     becomes a SECOND scheduler instance (separate
     `AsyncIOScheduler` against the v2 SQLAlchemy job
     store at `data/v2-scheduler.db`). v1 jobstore at
     `data/ori-scheduler.db` stays untouched.
   - Boot order: v1 boot → v2 `boot_runtime` (recovery →
     binding start → OneOff backfill → register active
     schedules → start worker pool) → adapters online.
   - Boot self-test (§6.7) **DEFERRED**: phase 9 ships
     `boot_runtime` without the admin-alert self-test
     gate. The self-test admin-alert path depends on
     `app/contracts/admin_alert.py` wiring that has
     phase-9 v1-path edits + a phase-10 audit-mirror
     follow-up. Phase 9 logs a `boot_runtime_complete`
     event; the self-test gate lands in a follow-up.

8. **`AuthoringToolset` mount on `CoordinatorAgent`** —
   `app/sub_agents/coordinator_agent.py` updated to add
   the toolset to its tool list. **Additive**: v1
   contract tools stay mounted alongside the v2 tools
   per §11.1 deprecation timeline ("legacy authoring
   tools deprecated with warnings; removed after ~60
   days"). The mount happens in the same commit that
   updates the Coordinator instruction text (§11.4
   MANDATORY).

9. **Coordinator instruction update** — per §11.4. The
   instruction text adds:
   - The "scheduling law" clause: every scheduled work
     item is created via a v2 typed tool; templates
     first; CustomFlow only when no template fits AND
     admin approval is in place.
   - References to the v2 template tool names
     (`OneOffReminder`, etc.) and the typed-tool path
     (`schedule_draft_start` → `schedule_dry_run` →
     `schedule_freeze`).
   - Deprecation note on v1 contract authoring (tools
     stay mounted for ~60 days but the Coordinator
     should prefer v2).

10. **Coordinator instruction guardrail test** — per
    §11.4 MANDATORY. New test file
    `tests/v2/test_coordinator_instruction_guardrail.py`
    parses the Coordinator's instruction template + the
    Coordinator agent module and asserts:
    - Does NOT contain `contract_freeze(spec:` /
      `contract_freeze(spec ` patterns (deprecated
      freeform signature).
    - DOES contain references to the v2 template tool
      names (`OneOffReminder`).
    - DOES contain the scheduling-law clause.
    - DOES contain `schedule_dry_run` / `schedule_freeze`
      / `schedule_draft_commit` / `schedule_create_reminder`
      string references.

11. **End-to-end pin** —
    `tests/v2/test_e2e_one_off_reminder.py`. Wires the
    DraftStore + HandshakeStore + DB + a stub Slack
    client + a stub channel cache + the v2 binding (in
    test mode, not real APScheduler) and asserts:
    - `schedule_create_reminder` → ok with
      `schedule_id`.
    - DB row exists; `schedule_created` event present.
    - Advancing the test clock past `at` and firing the
      wakeup callback inserts a Run.
    - The worker claims + executes the Run; the stub
      Slack client receives one `chat_postMessage` call
      with the expected channel + text.
    - `run_succeeded` event present in the EventLedger.

### Out of scope (phase 9)

- **Boot self-test (§6.7).** Admin-alert wiring depends
  on phase-10 audit-mirror; phase 9 logs a
  `boot_runtime_complete` event instead.
- **Compatibility worker (§11.1).** Legacy APScheduler
  jobs continue under their current callbacks unchanged.
  Synthetic-ledger wrapping lands in a later phase
  (likely phase 11 alongside the source-template work).
- **v1 → v2 migration tool (§11.2).** No
  `migrate_legacy_job_to_schedule_spec`; no v1 contract
  → ScheduleSpec import; no `apscheduler_jobs` callback
  rewiring.
- **v1 test removal / legacy authoring deprecation.**
  v1 tests stay green; v1 authoring tools stay mounted
  alongside v2.
- **Telegram emit.** Slack-only in phase 9. Telegram
  reminder emit lands in phase 10 (or whichever phase
  ships multi-channel emit).
- **Snoozable reminders.** `snoozable=True` in §5.2 is a
  later feature; phase 9 builds emits text and
  terminates.
- **Source loaders / source snapshots.** Phase 10.
- **ExecutionPlan body authoring.** Phases 10 / 12.
- **`RecurringSeriesFromSource` / `ChannelDigest`
  templates.** Phase 11 (after source loaders land).
- **`schedule_status` / `schedule_diff` / replay
  observability tools (§9).** Phase 15.

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
  to include `template.args` in the canonical hash.
- `app/v2/runtime/worker.py` — emit branch for
  template-only specs.
- `app/v2/toolsets/authoring.py` — add
  `schedule_create_reminder` `FunctionTool` +
  `ToolDescriptor`; `AuthoringToolset.get_tools` returns
  18 tools.
- `app/v2/authoring/__init__.py` — re-export
  `schedule_create_reminder`.
- `app/sub_agents/coordinator_agent.py` — toolset mount
  + instruction update.
- `run_bot.py` (or wherever the bootstrap lives) —
  `boot_runtime` call.
- `.v2-current-phase` — bump 8 → 9.
- `scripts/check_phase_scope.py` — `PHASE_ALLOWLIST[9]`
  + invariant-3 boundary shift.
- `docs/PHASE_9_PLAN.md` — this file.
- `docs/CONTRACTS_V2_DESIGN.md` — §5.2 amendment
  naming `TemplateRef.args`.

---

## 3. Module APIs

(See §1 for caller signatures; this section captures the
internal shapes the slice commits must hit.)

### 3.1 `TemplateRef` (amended)

```python
class TemplateRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    args: Optional[dict[str, Any]] = None  # NEW
```

- `args=None` for legacy spec round-trips (every
  pre-phase-9 spec on disk has no args).
- `args` participates in `ScheduleSpec.compute_hash` —
  same JSON-encoded path as the other fields. Pin via a
  regression test (before/after with the same args
  yields the same hash; different args yields a
  different hash).

### 3.2 `OneOffReminder` template

`app/v2/templates/one_off_reminder.py`:

- `TEMPLATE_NAME = "OneOffReminder"`, `TEMPLATE_VERSION = "1"`.
- `class OneOffReminderArgs(BaseModel)` — typed Pydantic
  for the args dict. Fields: `text: str (min_length=1,
  max_length=4000)`. Pin oversize.
- `build_one_off_reminder(...)` per §1 signature.
- Module hygiene pinned: no `datetime.now` /
  `uuid.uuid4` — clock + factories DI'd (phase-5 rule
  10).

### 3.3 `schedule_create_reminder` tool

`app/v2/authoring/templates.py`. Wraps the phase-7/8
pipeline. Failure-shape table:

| Step | Failure → ToolResponse code |
|---|---|
| parse `at` | `validation_failed(naive_at_datetime)` or `validation_failed(invalid_iso_datetime)` |
| resolve recipient channel | `cache_unavailable(slack_channels, network_error)` per phase-6 / `not_found` for unknown channel name |
| build template | `validation_failed(<template-specific code>)` |
| dry_run / freeze / commit | forwarded codes per phase-8 plan |

### 3.4 Slack emit adapter

`app/v2/emit/slack_reminder.py`:

- `class SlackPostResult(BaseModel)` — `ok: bool`,
  `channel: str`, `ts: Optional[str]`, `error:
  Optional[str]`.
- `SlackProtocol` — minimal protocol type carrying
  `chat_postMessage(channel: str, text: str) ->
  Awaitable[dict]`. Tests pass a stub.
- No imports of `slack_sdk` at module load (DI
  protocol-typed; phase-6 carry-forward).
- Errors caught + wrapped to `SlackPostResult(ok=False,
  error=str(exc))`. Worker emit branch surfaces
  per-FailurePolicy.

### 3.5 Worker emit branch

`app/v2/runtime/worker.py`: new branch on the existing
claim → execute pipeline. When the claimed Run carries
a spec with `template.name == "OneOffReminder"` AND
`execution_plan_hash is None`:

- Dispatch to `emit_reminder_to_slack(spec, slack_client,
  clock)`.
- On `ok=True`: append `run_succeeded` event +
  transition Run to `succeeded`.
- On `ok=False`: route per `spec.failure.on_failure_action`
  (phase 9 implements `alert_admin` and
  `abort_silent`; `retry_later` deferred to phase 10
  alongside the source loader → emit retry chain).

Existing branches (no template, no execution plan) raise
`UnsupportedSpecError` for now — phase 10 / 12 expand.

### 3.6 `_owner_default`

`app/v2/runtime/_owner_default.py`:

- Reads `V2_AUTHORING_OWNER_ID` env var ONCE at import
  time.
- Exposes `DEFAULT_AUTHORING_OWNER_ID: Optional[str]`.
- `AuthoringToolset.__init__` becomes: if explicit
  `expected_owner_id` is None → use the default. If
  both are None at startup → raise `RuntimeError` so
  the bot refuses to start instead of silently
  defaulting.
- Pin: env populated → default available; env missing
  → default is None.

### 3.7 `boot_runtime` integration

`run_bot.py` (or `app/agent.py`):

- Import + call `boot_runtime` AFTER the v1 scheduler
  has booted and BEFORE the Slack/Telegram poller comes
  online. The v2 binding uses its own
  `AsyncIOScheduler` instance backed by a separate
  `SQLAlchemyJobStore` pointing at
  `data/v2-scheduler.db`. v1's
  `data/ori-scheduler.db` stays untouched.
- Phase-5 lifecycle hooks (pause / resume / archive /
  revive) get bound to the v2 binding so phase-7
  lifecycle tools can fire them.
- Boot self-test (§6.7) NOT wired in phase 9. The
  binding starts unconditionally; future phase adds the
  admin-alert gate.

### 3.8 Coordinator mount + instruction

`app/sub_agents/coordinator_agent.py`:

- `AuthoringToolset(expected_owner_id=...)` instantiated
  with the env-default. Added to the agent's
  `tools=[…]` list.
- Instruction text gains the §11.4 scheduling-law
  clause and v2 tool references.

### 3.9 End-to-end test

`tests/v2/test_e2e_one_off_reminder.py`:

- Uses `tmp_path` for the v2 DB.
- Builds a Slack channel cache fixture covering the
  test channel.
- Stub Slack client records `chat_postMessage` calls.
- Wraps the test in a freezeable clock so `at` can be
  advanced.
- Pins the full sequence: tool call → DB row → wakeup
  fires (manually invoked, not real APScheduler) → Run
  claim → emit → `run_succeeded`.

---

## 4. Slice ordering + commit cadence

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan) | `docs/PHASE_9_PLAN.md` + `.v2-current-phase` 8 → 9 + `PHASE_ALLOWLIST[9]` + design §5.2 amend (`TemplateRef.args`) | regression: spec compute_hash with/without args |
| 1 | `TemplateRef.args` field + `compute_hash` extension | `test_models_common.py` (existing) update + new args round-trip pins |
| 2 | `OneOffReminder` template module + args model | `test_template_one_off_reminder.py` |
| 3 | `schedule_create_reminder` tool + toolset extension (18 tools) | `test_authoring_template_tool.py` + `test_authoring_toolset.py` count bump |
| 4 | Slack emit adapter + `SlackPostResult` | `test_emit_slack_reminder.py` |
| 5 | Worker emit branch + UnsupportedSpecError for non-template specs | `test_runtime_worker_emit_branch.py` |
| 6 | `_owner_default` + AuthoringToolset constructor wiring | `test_runtime_owner_default.py` + existing toolset constructor pins update |
| 7 | `boot_runtime` integration into `run_bot.py` (NO self-test) | existing `test_runtime_boot.py` extended; smoke pin that v1 jobstore stays separate |
| 8 | `CoordinatorAgent` mount + instruction update + guardrail test | `test_coordinator_instruction_guardrail.py` |
| 9 | End-to-end pin | `test_e2e_one_off_reminder.py` |
| closeout | acceptance walk + tag `v2-phase-9-complete` (gated on codex pass) | — |

10 slices is high; reviewer may bundle 1+2, 4+5, or 7+8.
Splitting kept explicit so each slice has a single
review surface; reviewer codex can collapse via
slice-cadence guidance during plan review.

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
- Naive `at` → `ValueError`.
- Empty `text` → Pydantic rejects (`min_length=1`).
- Oversize `text` (4001 chars) → Pydantic rejects.
- AST pin: no `datetime.now` / `uuid.uuid4`.

### 5.2 `test_authoring_template_tool.py`

- Happy path: schedule_create_reminder runs the full
  pipeline; returns `ok(schedule_id, spec)`; DB row +
  `schedule_created` event present.
- Naive `at` string → `validation_failed(
  naive_at_datetime)`.
- Invalid ISO → `validation_failed(invalid_iso_datetime)`.
- Channel cache miss + network down →
  `cache_unavailable`.
- Channel cache miss + network up + channel not found →
  `not_found`.
- Dry-run / freeze / commit forwarded codes pinned for
  one representative failure (e.g. body hash drift
  injection).
- Tool is registered on the AuthoringToolset under the
  name `schedule_create_reminder`; ToolDescriptor tags =
  `{DB_WRITE, FILESYSTEM_WRITE, READ_EXTERNAL,
  USES_OAUTH}` (DB + draft files + Slack lookup + OAuth
  via the cache resolver).

### 5.3 `test_emit_slack_reminder.py`

- Happy path: stub client receives one
  `chat_postMessage(channel, text)`; result is
  `SlackPostResult(ok=True, channel=..., ts=...)`.
- Stub raises → `SlackPostResult(ok=False, error=...)`.
- Missing `text` in `template.args` → result `ok=False,
  error="template.args missing 'text'"`.
- AST pin: no `slack_sdk` import at module load.

### 5.4 `test_runtime_worker_emit_branch.py`

- Run with OneOffReminder template + no plan → emit
  fires → `run_succeeded` event present;
  `Run.status == succeeded`.
- Stub emit returns `ok=False` + `failure.on_failure_
  action == alert_admin` → emit_failed + admin_alert_
  sent events present.
- Stub emit returns `ok=False` + `failure.on_failure_
  action == abort_silent` → run_failed event present,
  no admin alert.
- Run with no template AND no plan →
  `UnsupportedSpecError` (phase 10 / 12 implements
  CustomFlow without template).
- Run with execution_plan_hash set →
  `UnsupportedSpecError` for phase 9.

### 5.5 `test_runtime_owner_default.py`

- env populated → default returned; toolset constructor
  uses it as fallback.
- env missing → default is None; toolset constructor
  raises `RuntimeError` when explicit kwarg is also
  None.
- Explicit kwarg wins over env value.

### 5.6 `test_coordinator_instruction_guardrail.py`

- Parses the resolved Coordinator instruction text.
- Asserts: no `contract_freeze(spec:`,
  no `contract_freeze(spec ` substrings.
- Asserts: `OneOffReminder`, `schedule_dry_run`,
  `schedule_freeze`, `schedule_draft_commit`,
  `schedule_create_reminder` all referenced.
- Asserts: scheduling-law clause present (exact string
  fragment to be pinned in the test).

### 5.7 `test_e2e_one_off_reminder.py`

(See §3.9.) Full happy-path sequence pinned.

### 5.8 Carry-forward updates

- `test_authoring_toolset.py` — bump expected tool
  count 17 → 18; add `schedule_create_reminder` to
  `_EXPECTED_TAG_MATRIX`.
- `test_models_common.py` (or wherever TemplateRef
  round-trips are pinned) — add args round-trip cases.
- `test_validation.py` — args field is opaque to
  validate_schedule_spec; pin that it surfaces no
  issues on a OneOffReminder spec.
- `test_runtime_boot.py` — `boot_runtime` mounted but
  self-test not gated; pin via explicit assertion on
  the boot sequence.

---

## 6. CI guard checks

`scripts/check_phase_scope.py` gains:

```python
9: {
    # Existing v2 scope (carried forward)
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
    # NEW for phase 9 (cutover surface)
    "app/sub_agents/coordinator_agent.py",
    "run_bot.py",
    # Optional: app/agent.py if bootstrap lives there
    "app/agent.py",
},
```

**Forbidden-paths gate (`FORBIDDEN_PHASES_1_TO_8`):**
boundary shifts from "1 through 8" to "1 through 8" —
phase 9 lifts the gate per §12.1 invariant 3 (the
existing constant name is misleading post-renumber; we
keep the name but the `if phase >= 9` check already
short-circuits, so phase 9 may touch `app/contracts/`,
`app/tasks.py`, etc. **even though phase 9 explicitly
DOES NOT** edit them per §1 out-of-scope). The plan
keeps `FORBIDDEN_PHASES_1_TO_8` named as-is for clarity;
the runtime check is already correct.

**Recommendation (round-1 reviewer flag):** rename
`FORBIDDEN_PHASES_1_TO_8` → `FORBIDDEN_PHASES_PRE_CUTOVER`
to remove the off-by-one confusion. Defer the rename to
a follow-up commit if reviewer agrees the name change
is non-scope for phase 9.

Cross-cutting smoke checks (carry-forward):

- Phase-9 NEW modules import no I/O libs at module load
  (`slack_sdk` / `googleapiclient` / `httpx` /
  `requests` / `urllib3` / `aiohttp` / `smtplib` /
  `subprocess`). Slack client is protocol-typed DI per
  phase-6 carry-forward.
- Phase-9 NEW modules do NOT import
  `app.v2.runtime._defaults` at module load. Clock +
  id factories stay DI.
- `_defaults.py` remains the ONLY runtime / authoring
  module whose smoke test asserts `uuid` /
  `datetime.now` imports.
- `_owner_default.py` IS allowed to import `os.environ`
  at module load (it explicitly reads env once); pinned
  via an explicit exception in the import-hygiene
  helper.

---

## 7. Acceptance criteria for `v2-phase-9-complete`

1. Branch ahead of `v2-phase-8-complete` by N small
   commits, each scoped to one slice in §4.
2. `TemplateRef.args` round-trips through DraftStore +
   ScheduleStore + `ScheduleSpec.compute_hash`; pre-args
   specs (args=None) still round-trip unchanged.
3. `build_one_off_reminder` returns a frozen spec with
   the documented shape; naive `at` rejected; empty /
   oversize text rejected.
4. `schedule_create_reminder` happy-path lands a
   schedule + `schedule_created` event; failure
   shapes documented in §5.2 all pinned.
5. Slack emit adapter sends `chat_postMessage` with the
   documented (channel, text) and surfaces a typed
   `SlackPostResult`.
6. Worker emit branch fires on OneOffReminder specs;
   `run_succeeded` event written on emit success;
   FailurePolicy routing pinned for `alert_admin` +
   `abort_silent`.
7. `_owner_default` env fallback works; explicit kwarg
   wins; both-None raises `RuntimeError` at startup.
8. `boot_runtime` is called from `run_bot.py` after v1
   boot; v2 binding starts; lifecycle hooks wired so
   phase-7 lifecycle tools can fire them.
9. `AuthoringToolset` mounted on `CoordinatorAgent`;
   Coordinator instruction text gains the §11.4
   scheduling-law clause; guardrail test green.
10. End-to-end test: `schedule_create_reminder` → DB +
    event → wakeup fires → worker claims + emits → Slack
    stub receives one call → `run_succeeded` event.
11. Phase guard `--diff v2-phase-8-complete` clean.
12. v1 paths NOT touched in any phase-9 commit (out of
    scope per §1; the v1-path gate lifts but is not
    exercised).
13. No `datetime.now()` / `uuid.uuid4()` outside
    `_defaults.py` (except the documented
    `_owner_default.py` env read). AST pin on every
    phase-9 NEW module.
14. Full v2 test suite passes (existing 1696 + phase-9
    adds); no regressions.
15. Coordinator instruction guardrail test asserts the
    v1 freeform signature is absent + v2 tool names are
    present.
16. Annotated git tag `v2-phase-9-complete` created and
    pushed (workflow pre-approved per phase 5–8
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

TemplateRef gains an optional args dict so emit-only
templates can carry per-instance payload (reminder text)
without an ExecutionPlan. ScheduleSpec.compute_hash
includes args so a body change re-hashes; pre-args specs
round-trip unchanged via args=None.

expected_owner_id picks up V2_AUTHORING_OWNER_ID env
fallback (deferred from phase 7 round-2 L365); explicit
kwarg still wins; both-None raises a startup error.

Coordinator instruction gains the §11.4 scheduling-law
clause + v2 tool references. v1 authoring tools stay
mounted alongside v2 per the deprecation timeline.

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

### 9.1 Closed in this revision

None — everything in this draft is provisional pending
round-1 reviewer.

### 9.2 Open

1. **TemplateRef.args (§0)**: option (a) field on
   TemplateRef is proposed. Reviewer to confirm vs (b)
   top-level `ScheduleSpec.static_emit_body` or (c)
   synthetic minimal ExecutionPlan. Knock-on:
   `ScheduleSpec.compute_hash` ordering of the new
   field; round-trip with pre-phase-9 specs (which lack
   the field).
2. **Boot self-test (§6.7)**: defer to phase 10 or
   ship in phase 9 alongside `boot_runtime` wiring?
   Plan proposes defer. Reviewer to confirm.
3. **Compatibility worker (§11.1)**: defer to a later
   phase (proposal) or ship in phase 9 so v1 jobs
   surface in the v2 ledger from the cutover boundary?
   Plan proposes defer. Trade-off: observability
   parity vs phase-9 scope.
4. **AuthoringToolset mount**: additive (v1 tools stay
   mounted) per §11.1 deprecation timeline (proposal),
   or replacement (v1 tools removed in phase 9) for a
   harder cutover? Plan proposes additive.
5. **Telegram emit**: defer to phase 10 (proposal), or
   ship Slack + Telegram together in phase 9? Plan
   proposes Slack-only.
6. **`schedule_create_reminder` return shape**:
   pipeline-internal `ok(schedule_id, spec)` (proposal,
   wraps the whole flow), or staged so the LLM gets
   `ok(draft_id)` from each step and drives the
   pipeline itself? Plan proposes wrapped — the LLM
   sees one tool call returning a schedule. Less LLM
   churn; less inspection ability.
7. **PHASE_ALLOWLIST[9] scope**: the proposed widening
   covers `app/sub_agents/coordinator_agent.py`,
   `run_bot.py`, and (optionally) `app/agent.py`.
   Reviewer to confirm — should the widening also
   cover `app/contracts/__init__.py` or any v1 file
   for the §11.4 instruction-source files? Plan
   keeps it tight.
8. **`FORBIDDEN_PHASES_1_TO_8` rename**: keep name
   (proposal — defer to a follow-up commit) or rename
   to `FORBIDDEN_PHASES_PRE_CUTOVER` inside phase 9 to
   remove the post-renumber confusion?
9. **v2 jobstore location**: `data/v2-scheduler.db`
   (proposal; sibling to v1's `data/ori-scheduler.db`)
   or a different filename / directory? Plan proposes
   sibling to keep deploy paths simple.
10. **End-to-end test timing model**: real APScheduler
    + sleep (slow, flaky), or stub `boot_runtime` and
    invoke the wakeup callback synchronously
    (proposal)? Plan proposes synchronous-stub for
    determinism; a `@pytest.mark.slow` real-APScheduler
    test could land in a later phase if reviewer wants
    additional coverage.

---

## 10. Hard rules (carried forward)

Same as phase 8 plan §10. Restated for self-containment:

1. No push without explicit reviewer / Sergey approval.
2. No edits to phase-1 through phase-8 plan docs
   without a `PHASE_OVERRIDE:` mechanism in the commit
   message. The §0 design amendment for
   `TemplateRef.args` requires `PHASE_OVERRIDE:` on the
   plan-landing commit.
3. No time estimates.
4. Pause after each commit for reviewer.
5. `uv run python …` always.
6. Pre-commit hook needs `.docs_read_marker` —
   `echo "yes" | uv run python scripts/check_docs_read.py`.
7. Commit messages end with
   `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
8. Leave `app/tools/youtube.py` dirty/uncommitted unless
   reviewer flags otherwise; same for the three diag
   scripts at the repo root.
9. **v1 paths may be touched starting phase 9** per
   §12.1 invariant 3 (boundary lift). Phase 9 explicitly
   does NOT touch them; phase 10+ will.
10. Every runtime / authoring helper takes injected
    clock + id factories; `_defaults.py` is the ONLY
    module that wires them to wall clock + uuid4.
    `_owner_default.py` is the documented exception
    that reads env once at import time.
11. **AuthoringToolset mount is ADDITIVE** in phase 9
    per §11.1 deprecation timeline; v1 contract tools
    stay mounted on the Coordinator alongside v2 until
    the ~60-day sunset.
