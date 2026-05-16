# Phase 11 plan — source-driven fire-path cutover + `RecurringSeriesFromSource`

`docs/CONTRACTS_V2_DESIGN.md` §12 **step 11** ("Source
templates: `RecurringSeriesFromSource`, `ChannelDigest`.
Phase-1 completion."; deps: step 10 + step 9) + §5.2
(template-first authoring) + §5.3 (source-driven content)
+ §5.3.5 (per-fire snapshot) + §11.1/§11.4 (additive
cutover + agent-guidance migration).

Phases 5–10 BUILT the source layer (loaders / snapshot
writer / cache / resolver) but deliberately did NOT wire
it into the worker fire path (build-the-layer; worker
still raises `UnsupportedSpecError` on
`execution_plan_hash`, `worker.py:454`). **Phase 11 is the
CUTOVER**: `resolve_source` goes live in the worker fire
path; a source-driven `ScheduleSpec` actually fires.

> **Reviewer note (read §1 + §9 first).** Both §12-step-11
> templates have a HARD forward dependency on a subsystem
> that is NOT built and is NOT cleanly owned by step 11:
> `ChannelDigest(…, summary_prompt)` needs an LLM
> `ReasoningStep` chain executor (no such executor exists
> in `app/v2/runtime/`; step 12 is reasoning *enforcement*,
> which presupposes the executor), and
> `RecurringSeriesFromSource(…, progress_strategy)` for a
> "next unread item" series needs cross-fire
> `schedule_state` (`state_read`+pick+`state_write` = §12
> **step 13**, NOT built). The dependency-clean,
> load-bearing deliverable is the **fire-path cutover +
> the emit-only source path + a STATELESS
> `RecurringSeriesFromSource`**. The disposition of
> `ChannelDigest` and of the stateful progress strategy is
> the central open question — see §0 + §9.

---

## 0. Proposed §12 step-11 refinement (PENDING reviewer — NOT yet a design edit)

Mirrors the phase-10 amendment discipline: the plan
PROPOSES a scope refinement; `docs/CONTRACTS_V2_DESIGN.md`
is amended ONLY after the reviewer approves the
disposition (plan≡design stays load-bearing — no silent
design edit in the plan commit).

**Proposed:** step 11 ships in two parts; phase 11 = part
A (this plan). Part B (`ChannelDigest` + stateful series)
defers to a later phase that depends on the reasoning
executor + step 13, because shipping a non-summarising
"digest" is a degraded product the user did not ask for
and a stateful series without `schedule_state` is unsafe
(no CAS, lost-progress on retry).

- **11A (this phase):** fire-path cutover; emit-only
  source delivery; `RecurringSeriesFromSource` with
  STATELESS progress strategies only.
- **11B (deferred):** `ChannelDigest` + `summary_prompt`
  (needs reasoning executor); stateful
  `RecurringSeriesFromSource` progress (needs step 13
  cross-fire state).

Reviewer adjudicates the split in plan-review round 1
(§9 Q1/Q2). If the reviewer rejects the split, the slice
plan in §4 expands accordingly.

---

## 1. Scope statement

### In scope (phase 11 / 11A)

1. **Worker fire-path cutover.** `Worker._dispatch_emit_branch`
   (`app/v2/runtime/worker.py`) stops raising
   `UnsupportedSpecError` for a source-driven spec. A
   source-driven `ScheduleSpec` =
   `execution_plan_hash` set → an `ExecutionPlan` whose
   `inputs` carry `InputSpec.source_ref` and whose
   `emits` are emit-only, with **zero `ReasoningStep`s**.
   The worker loads the plan
   (`get_execution_plan(conn, hash)`), resolves every
   source-bearing input, then runs the emit step(s).
2. **`resolve_source` wired in (the cutover core).** Per
   `InputSpec` with a `source_ref`, the worker calls the
   phase-10 `resolve_source(...)` on the claimed
   connection, BEFORE emit. The phase-10 contract is
   honoured verbatim: it returns EXACTLY ONE terminal
   `ResolveOutcome` and NEVER raises into the worker.
   - `RESOLVED` → content flows to emit.
   - `DRIFT` (`alert_on_shape_change`) → content STILL
     served (drift event already emitted by the resolver).
   - `FAILED` → the run fails via the failure policy
     (`require_reapprove_on_shape_change` withholds
     content → `FAILED`; the FRESH snapshot row/file is
     still written by the resolver as audit). No partial
     emit.
3. **Emit-only source delivery.** A new emit adapter
   posts the resolved content to the target channel
   (Slack phase-1). Mirrors the phase-9 emit shape; the
   `OneOffReminder` path is UNTOUCHED (additive cutover,
   §11.1 discipline).
4. **`RecurringSeriesFromSource` template** (`source`,
   `channel`, `hour_local`, `timezone`,
   `progress_strategy`) — STATELESS strategies only:
   `whole` (emit the full resolved content each fire) and
   `skip_unchanged` (emit only when `content_hash`
   differs from the immediately-prior materialised
   snapshot — reuses the phase-10 prior-snapshot
   machinery, NO new state table). Builder + typed args
   model mirror `build_one_off_reminder` /
   `OneOffReminderArgs`. Authoring tool mirrors
   `make_schedule_create_reminder`; it compiles a
   `ScheduleSpec` + `ExecutionPlan`
   (`inputs=[source_ref]`, `emits=[source_post]`,
   `reasoning=[]`).
5. **Failure routing.** Resolve/emit failure routes
   through the existing `_route_failure_policy`
   (generalised from `SlackPostResult` to a typed
   resolve/emit failure). `retry_later` stays downgraded
   to `alert_admin` with a WARNING (the retry chain is
   step 14, mirrors the phase-9 decision).
6. **Agent guidance (§11.4) additive.** Coordinator
   instruction + scheduling-law: the template list gains
   `RecurringSeriesFromSource`. Additive only — no v1
   surface removed, no `OneOffReminder` text changed
   beyond adding the new template name.
7. **CI / hygiene parity.** `tests/v2/test_phase11_import_hygiene.py`
   AST pin on every phase-11 NEW module (mirror phase-9/10).
   The `PHASE_ALLOWLIST[11]` + `.v2-current-phase`→`11`
   phase-transition flip lands in the PLAN commit itself
   (the scope guard requires the plan/transition commit be
   scope-clean for its own diff — the phase-10 cadence);
   `gen_docs.py` regen + stage at closeout.

### Out of scope (phase 11) — deferred with rationale

- **LLM `ReasoningStep` chain executor** — no §12 step
  owns it cleanly (step 12 = read-only *enforcement*,
  presupposes the executor). A source-driven plan with
  a non-empty `reasoning` list → typed
  `UnsupportedSpecError` (step-12 boundary, mirrors the
  phase-9/10 boundary discipline).
- **`ChannelDigest` + `summary_prompt`** — depends on the
  reasoning executor (11B, §0).
- **Stateful `RecurringSeriesFromSource` progress**
  (`state_read`+pick+`state_write`) — depends on §12
  step 13 cross-fire `schedule_state` (11B, §0).
- **Retry chain** (step 14), **idempotency/cancellation**
  (step 14), **v1→v2 migration tooling** (step 16),
  **observability tools** (step 15). Unchanged.
- **v1 scheduler** — untouched (`app/contracts/*`,
  `app/tasks.py`, `app/scheduler_instance.py`,
  `data/contracts/*`). The phase-9 cutover already moved
  the §12.1-invariant-3 boundary; phase 11 adds no v1 edit.

### Invariants the cutover MUST preserve (carry-forward, codex-approved phases 5–10)

- §3.5 typed-error taxonomy + `fallback_eligible`
  ClassVar: ONLY `SourceFetchError` is fallback-eligible;
  `SourceAuth/Security/Policy/ParseError` →
  non-fallback, never cache/fallback/serve-stale.
- Resolver contract: EXACTLY ONE terminal
  `ResolveOutcome`; NEVER raises into the caller; the
  terminal event row is best-effort
  (`event_emitted=False` possible) and never flips the
  terminal status. The worker treats a non-`RESOLVED`
  outcome as a clean control-flow signal, never an
  exception.
- Cache `cache_ttl_seconds > 0` = cross-fire NO-PROBE
  window (a `CACHE_HIT` is legitimate even if the live
  source would now fail); `== 0` = probe-every-fire
  security opt-out. The worker does NOT re-implement or
  bypass this — it calls the resolver, which owns it.
- §5.3.1 local-file dirfd `O_NOFOLLOW|O_NONBLOCK`
  swap-proof fence — unchanged; the worker never opens a
  source path itself.
- §5.3.5 per-fire snapshot: `.bin` holds canonical
  `content_bytes` VERBATIM, `sha256(file)==content_hash`;
  metadata only in the `source_snapshots` row; the
  snapshot is written by the resolver/cache path, NOT by
  the worker (Q5). Retention = `prune_snapshots`
  commit-then-unlink (unchanged).
- Sources are READ-ONLY (§5.3.6); verbatim bytes
  (§5.3.7) — the emit adapter delivers the resolved
  bytes/text faithfully, no mutation of the source.
- No `datetime.now` / `uuid.uuid4` / vendor-SDK at module
  load outside the sanctioned sites (`_defaults.py`;
  `_owner_default.py` env-read exception) — phase-11
  NEW modules AST-pinned. Slack transport stays DI
  (`SlackProtocol`), no `slack_sdk` at module load.
- Additive cutover (§11.1): the `OneOffReminder` emit
  branch and v1 scheduler keep working unchanged.
- plan ≡ design ≡ tag-annotation, zero divergence (the
  phase-9/10 stale-wording lesson — one reconciliation
  pass at closeout).

---

## 2. New file paths

- `app/v2/emit/source_post.py` — `SourcePostResult` +
  `emit_source_to_slack(...)` (reuses the phase-9
  `SlackProtocol`; no `slack_sdk` at module load).
- `app/v2/templates/recurring_series_from_source.py` —
  `RECURRING_SERIES_FROM_SOURCE_TEMPLATE_NAME`,
  `RecurringSeriesFromSourceArgs` (typed
  `TemplateRef.args`), `build_recurring_series_from_source(...)`.
- `tests/v2/test_phase11_import_hygiene.py` — AST pin
  (mirror `test_phase10_import_hygiene.py`).
- Test files (§5): `tests/v2/test_runtime_source_fire.py`,
  `tests/v2/test_emit_source_post.py`,
  `tests/v2/test_templates_recurring_series_from_source.py`,
  extensions to
  `tests/v2/test_runtime_worker_emit_branch.py` and the
  authoring-templates test.

**Edited (non-new):** `app/v2/runtime/worker.py`
(`_dispatch_emit_branch` source-driven branch),
`app/v2/authoring/templates.py`
(`make_schedule_create_recurring_series_from_source`),
`app/sub_agents/coordinator_agent.py` (additive template
list + §11.4 scheduling-law), `scripts/check_phase_scope.py`
(`PHASE_ALLOWLIST[11]`), `.v2-current-phase`,
`docs/CONTRACTS_V2_DESIGN.md` (§12 refinement —
ONLY after reviewer approves §0, in a slice-fix, never
in the plan commit).

---

## 3. Module APIs (sketch — finalised per slice)

### 3.1 Worker source-driven branch (`worker.py`)

In `_dispatch_emit_branch`, after the existing
deleted/inactive/staleness checks, BEFORE the
`execution_plan_hash is not None → raise`:

```
if spec.execution_plan_hash is not None:
    plan = get_execution_plan(conn, spec.execution_plan_hash)
    if plan is None: -> _fail_run(reason="execution_plan_missing_at_claim")
    if plan.reasoning:  # step-12 boundary
        raise UnsupportedSpecError("reasoning steps land in step 12")
    for inp in plan.inputs:
        if inp.source_ref is None: continue
        outcome = await resolve_source(ref=inp.source_ref, ...,
                                       conn=conn, conn_factory=..., clock=...,
                                       event_id_factory=..., audit=spec.audit)
        if outcome.status is FAILED:
            -> _route_failure_policy(... typed resolve failure ...); return "failed"
        # RESOLVED or DRIFT(alert) -> carry outcome.content_bytes
    result = await emit_source_to_slack(spec=spec, plan=plan,
                                        resolved=<bytes/text>, slack_client=..., clock=...)
    if result.ok: return "succeeded"
    -> _route_failure_policy(...); return "failed"
```

`resolve_source` is called with the worker's DI clock /
`event_id_factory` / `conn_factory` (NOT
`datetime.now`/`uuid4`). The resolver owns the snapshot
write (Q5). Exactly-one-terminal-outcome / never-raise
means the worker has NO `try/except` around it for
control flow — it switches on `outcome.status`.

### 3.2 `emit_source_to_slack` (`emit/source_post.py`)

Mirrors `emit_reminder_to_slack`:
`async emit_source_to_slack(*, spec, plan, resolved,
slack_client: SlackProtocol, clock) -> SourcePostResult`.
`SourcePostResult(ok: bool, channel: str, ts: Optional[str],
error: Optional[str])`. Channel + format come from the
`source_post` EmitStep `args` compiled by the template.
Verbatim text preservation (§5.3.7) — no reformat of the
resolved bytes beyond the template-declared envelope.

### 3.3 `RecurringSeriesFromSource` (`templates/…`)

`build_recurring_series_from_source(*, source_ref_args,
channel, hour_local, timezone, progress_strategy, clock,
schedule_id_factory) -> (ScheduleSpec, ExecutionPlan)`.
`progress_strategy ∈ {"whole", "skip_unchanged"}`
(STATELESS; Q2). Compiles:
- `ExecutionPlan(inputs=[InputSpec(id="src",
  source_ref=SourceRefSpec(...))], reasoning=[],
  emits=[EmitStep(id="post", adapter="source_post",
  args={channel, progress_strategy})])`.
- `ScheduleSpec(trigger=Cron(hour_local, timezone),
  execution_plan_hash=plan.compute_hash(),
  template=TemplateRef(name=RECURRING_…_NAME,
  args=RecurringSeriesFromSourceArgs(...).model_dump()))`.
`skip_unchanged` is realised in the worker emit step:
compare `outcome.content_hash` to the prior materialised
snapshot hash (the resolver already reads it for
live-change); equal → no-op success
(`run_succeeded` with `emit_skipped_unchanged` event),
no Slack post. NO new state table.

### 3.4 Authoring tool (`authoring/templates.py`)

`make_schedule_create_recurring_series_from_source(*, store,
handshake_store, conn_factory, …, clock, event_id_factory,
schedule_id_factory, owner, session_id)` — LLM-visible
signature is the slot set only (e.g.
`(source, channel, hour_local, timezone,
progress_strategy)`); DI threaded behind it (mirror the
phase-9 `make_schedule_create_reminder` DI-leak pin).
Funnels through the same `validate_schedule_spec` +
dry-run handshake + freeze + commit pipeline.

---

## 4. Slice ordering + commit cadence

Slice-gated; pause after each commit for reviewer; no
push mid-phase. Cadence mirrors phases 9/10.

0. **plan + phase transition** (this commit; NOT pushed)
   — `docs/PHASE_11_PLAN.md`, `PHASE_ALLOWLIST[11]` in
   `scripts/check_phase_scope.py`, `.v2-current-phase`→11.
   The transition flip MUST be in this commit so the
   scope guard accepts the plan commit's own diff
   (phase-10 cadence). Design doc NOT touched here.
1. **Worker plan-load + source-driven routing** — load
   `ExecutionPlan`; route inputs+emits-only plans into a
   new branch; typed `UnsupportedSpecError` for
   reasoning-bearing plans (step-12 boundary) +
   `execution_plan_missing_at_claim` fail; pin "resolver
   owns the snapshot" (Q5). NO resolve / NO emit yet
   (the branch raises a `not-yet-wired` typed error so
   nothing fires) — purely the routing skeleton + tests.
2. **`resolve_source` wired in** — call the resolver per
   source-bearing input on the claimed conn; map
   `ResolveOutcome` → continue / fail; honour
   exactly-one-outcome / never-raise; FAILED &
   require_reapprove → failure policy; DRIFT(alert) →
   carry content. No emit yet (stop after resolve,
   succeed with a `source_resolved_no_emit` test hook).
3. **`emit/source_post.py`** — `SourcePostResult` +
   `emit_source_to_slack`; wire the resolved bytes →
   Slack; `OneOffReminder` path untouched; full
   resolve→emit→succeeded path live for `whole`.
4. **`RecurringSeriesFromSource` builder** —
   `templates/recurring_series_from_source.py`; typed
   args; `whole` + `skip_unchanged`; `skip_unchanged`
   no-op-success emit path + `emit_skipped_unchanged`
   event.
5. **Authoring tool** —
   `make_schedule_create_recurring_series_from_source`;
   ExecutionPlan compile (inputs+emit, zero reasoning);
   validation/dry-run/freeze/commit end-to-end.
6. **Failure-policy integration** — generalise
   `_route_failure_policy` to the typed resolve/emit
   failure; `alert_admin`/`abort_silent`; `retry_later`
   → `alert_admin` + WARNING (step-14 note); end-to-end
   FAILED / require_reapprove / DRIFT pins.
7. **Agent guidance + hygiene** — additive coordinator
   §11.4 scheduling-law (template list +
   `RecurringSeriesFromSource`);
   `test_phase11_import_hygiene.py` AST pin.
   (`PHASE_ALLOWLIST[11]` / `.v2-current-phase` already
   flipped in slice 0; this slice only EXTENDS the
   allowlist if a new surface path appeared.)
8. **closeout** — full `tests/v2`, §7 acceptance walk,
   phase guards, annotated tag `v2-phase-11-complete`
   (gated on reviewer CLOSEOUT PASS), §0 design
   amendment to `CONTRACTS_V2_DESIGN.md` IF the reviewer
   approved the split.

---

## 5. Test inventory (highlights)

- `test_runtime_source_fire.py` — source-driven spec
  fires end to end (RESOLVED→emit→succeeded);
  reasoning-bearing plan → typed `UnsupportedSpecError`;
  missing plan → `execution_plan_missing_at_claim`;
  resolver FAILED → `run_failed` via failure policy, NO
  emit, NO partial; `require_reapprove` → withhold +
  FAILED + snapshot still written; DRIFT(alert) → drift
  event + STILL emits; resolver called with DI
  clock/id-factory (not `datetime.now`/`uuid4`); resolver
  never-raise honoured (a forced internal resolver fault
  → single `SOURCE_FAILED` outcome, worker fails
  cleanly, no crash).
- `test_emit_source_post.py` — `emit_source_to_slack`
  ok / not-ok; verbatim bytes preserved; channel from
  EmitStep args; `OneOffReminder` emit unaffected.
- `test_templates_recurring_series_from_source.py` —
  builder produces a valid `ScheduleSpec`+`ExecutionPlan`
  (zero reasoning, one source input, one source_post
  emit); `skip_unchanged` no-ops on equal hash
  (`emit_skipped_unchanged`), emits on changed hash;
  naive datetime / bad slot rejected at the builder.
- `test_runtime_worker_emit_branch.py` (extend) — the
  source-driven branch coexists with the OneOff branch;
  the phase-9 `schedule_not_found` / inactive / template
  invariants still hold.
- `test_phase11_import_hygiene.py` — every phase-11 NEW
  module: no module-load `_defaults` / `slack_sdk` /
  `google` / `httpx` import; no `uuid.uuid4` /
  `datetime.now` call; helper self-tests.
- Authoring-templates test (extend) — the new tool
  funnels through `validate_schedule_spec` + dry-run +
  freeze; LLM-visible signature is the slot set only
  (DI-leak pin).

Test-first per §11.3: each slice ships its test file in
the same commit.

---

## 6. CI guard checks

- `scripts/check_phase_scope.py --staged` and
  `--diff v2-phase-10-complete` exit 0;
  `PHASE_ALLOWLIST[11]` enumerates exactly the phase-11
  surface (worker.py, emit/source_post.py,
  templates/recurring_series_from_source.py,
  authoring/templates.py, coordinator_agent.py — the
  §11.4 carry, like phase 9 — `.v2-current-phase`,
  the docs, the test files). `phase >= 9` doc-coupling
  applies (behaviour change paired with the matching
  hand-written doc; `ORI_SKIP_DOC_CHECK` is
  harness-DENIED — never bypass).
- AST import-hygiene pin (above).
- `gen_docs.py` regen at closeout; stage
  `docs/INDEX.md` / `docs/AGENTS_INVENTORY.md` only if
  `files_changed != 0` (a new template/adapter MAY be an
  indexed symbol — unlike phase-10 sources — so a
  non-zero diff is expected here; stage it).
- Full `tests/v2` green, no regression (2185 baseline +
  phase-11 adds).

---

## 7. Acceptance criteria for `v2-phase-11-complete`

1. Branch ahead of `v2-phase-10-complete` by N small
   per-slice commits.
2. A source-driven `ScheduleSpec` (execution_plan_hash →
   ExecutionPlan with `InputSpec.source_ref`, zero
   reasoning) fires end to end: claim → resolve → emit →
   `succeeded`, with a `source_snapshots` row + `.bin`
   written by the resolver path.
3. The worker calls `resolve_source` with DI
   clock/id-factory; the phase-10 resolver contract is
   unbroken (exactly one terminal outcome; never raises
   into the worker — pinned with a forced internal
   resolver fault).
4. Resolver `FAILED` → `run_failed` via failure policy,
   no emit, no partial side effect;
   `require_reapprove_on_shape_change` → content
   withheld + `FAILED` + FRESH snapshot still written;
   `DRIFT`(`alert_on_shape_change`) → drift event +
   content STILL emitted.
5. Non-fallback source errors
   (`SourceAuth/Security/Policy/ParseError`) never serve
   stale cache through the worker (the §3.5 invariant
   holds across the cutover).
6. `emit_source_to_slack` delivers the resolved bytes
   VERBATIM (§5.3.7); the `OneOffReminder` emit path is
   byte-for-byte unchanged (additive cutover).
7. `RecurringSeriesFromSource` builder + authoring tool
   produce a valid spec; `whole` emits each fire;
   `skip_unchanged` no-ops (`emit_skipped_unchanged`)
   when `content_hash` is unchanged, emits when changed;
   NO new state table introduced.
8. A reasoning-bearing ExecutionPlan → typed
   `UnsupportedSpecError` (step-12 boundary held); a
   missing plan → `execution_plan_missing_at_claim`.
9. No `datetime.now` / `uuid.uuid4` / vendor-SDK
   module-load in any phase-11 NEW module; AST pin green.
10. v1 untouched; coordinator change is ADDITIVE only
    (template list + scheduling-law; no v1 surface
    removed; `OneOffReminder` guidance unchanged beyond
    the added template name).
11. Phase guard `--staged` + `--diff v2-phase-10-complete`
    exit 0.
12. `cache_ttl_seconds` semantics honoured end to end (a
    within-TTL `CACHE_HIT` fires from the snapshot
    without a loader probe; `== 0` probes every fire) —
    pinned through the worker path.
13. Full `tests/v2` green (2185 baseline + phase-11
    adds); no regression. §7 walked with evidence.
14. plan ≡ shipped code ≡ design ≡ tag annotation, zero
    divergence (one reconciliation pass at closeout).
15. Annotated tag `v2-phase-11-complete` created (push
    gated on reviewer CLOSEOUT PASS, phases 5–10 pattern).

---

## 8. Tag annotation (draft — finalised at closeout)

```
v2 phase 11 complete

Source-driven fire-path cutover. The phase-10 source
layer (loaders / snapshot / cache / resolver), built but
unwired, is now LIVE in the worker fire path: a
source-driven ScheduleSpec (execution_plan_hash ->
ExecutionPlan with InputSpec.source_ref, zero reasoning)
fires end to end. The worker loads the ExecutionPlan,
calls resolve_source per source-bearing input on the
claimed connection with DI clock/id-factory, and emits
the resolved content. The phase-10 resolver contract is
unbroken: exactly one terminal outcome, never raises into
the worker; RESOLVED -> emit, DRIFT(alert) -> drift event
+ still emit, FAILED / require_reapprove -> run_failed via
the failure policy with content withheld and the FRESH
snapshot still written as audit. Non-fallback source
errors never serve stale cache through the worker; the
cache no-probe / probe-every-fire ttl semantics and the
per-fire .bin verbatim snapshot are owned by the resolver
path, not re-implemented in the worker.

emit/source_post.py delivers the resolved bytes VERBATIM
to Slack (SlackProtocol DI, no slack_sdk at module load);
the OneOffReminder emit path is byte-for-byte unchanged
(additive cutover, v1 scheduler untouched).
RecurringSeriesFromSource (template + authoring tool)
ships with STATELESS progress strategies only (whole /
skip_unchanged via the prior-snapshot content_hash; no
new state table). A reasoning-bearing plan -> typed
UnsupportedSpecError (step-12 boundary held).

NOT shipped (deferred): ChannelDigest + summary_prompt
(needs the LLM reasoning-chain executor); stateful
RecurringSeriesFromSource progress (needs step-13
cross-fire schedule_state); retry chain (step 14);
v1->v2 migration tooling (step 16). §12 step 11 is split
into 11A (this phase) + 11B (deferred) per the
reviewer-approved §0 refinement.

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 11, §5.2,
        §5.3, §5.3.5, §11.1/§11.4
Plan:   docs/PHASE_11_PLAN.md
```

---

## 9. Open questions (for plan-review round 1)

- **Q1 — `ChannelDigest` disposition.** Options: (a)
  defer `ChannelDigest` entirely to 11B (post reasoning
  executor) — RECOMMENDED (a non-summarising concat
  "digest" is a degraded product the user didn't ask
  for); (b) ship a deterministic non-LLM multi-source
  concat/format digest now, `summary_prompt` later; (c)
  pull the LLM reasoning-chain executor into phase 11
  (large scope expansion; collides with step 12). The
  plan assumes (a). Adjudicate.
- **Q2 — `RecurringSeriesFromSource` progress
  strategies.** Proposal: STATELESS only — `whole` +
  `skip_unchanged` (reuses the phase-10 prior-snapshot
  `content_hash`, NO new state). The stateful "next
  unread item" strategy defers to 11B (step-13
  cross-fire `schedule_state`). Accept the stateless-only
  set for phase 11?
- **Q3 — emit adapter.** New `emit/source_post.py`
  (reuse `SlackProtocol`) vs generalising
  `emit_reminder_to_slack`. Proposal: NEW adapter so the
  phase-9 OneOff emit stays byte-for-byte untouched
  (additive cutover, §11.1). Agree?
- **Q4 — reasoning-bearing plan handling.** Proposal: a
  plan with non-empty `reasoning` → typed
  `UnsupportedSpecError` ("reasoning lands in step 12"),
  mirroring the phase-9/10 boundary discipline (the Run
  stays RUNNING, recovery promotes it). Agree this is
  the correct step-12 boundary, vs `_fail_run`?
- **Q5 — snapshot write ownership.** The plan assumes
  `resolve_source` / `resolve_source_cached` already
  persists the per-fire `.bin` + `source_snapshots` row,
  so the worker MUST NOT call `write_snapshot` itself
  (double-write / dedup hazard). Slice 1 pins this.
  Confirm the resolver owns the snapshot and the worker
  is a pure consumer.
- **Q6 — `_route_failure_policy` reuse.** Generalise the
  phase-9 helper (currently typed to `SlackPostResult`)
  to a shared resolve/emit failure shape, vs a parallel
  helper. Proposal: generalise (one failure-routing
  path). Acceptable, or keep them separate for blast-radius
  containment?
- **Q7 — slice count.** 8 slices (0 plan … 8 closeout)
  proposed. If Q1 selects (b)/(c) the count grows. OK at
  8 for 11A?

---

## 10. Hard rules (carried forward from phases 9–10)

1. Reviewer (now claude-reviewer) via Sergey relay;
   slice-gated; pause after each commit; no push
   mid-phase; closeout tag+branch push gated on a
   reviewer CLOSEOUT PASS (phases 5–10 auto-push-on-PASS
   pattern).
2. `uv run python …` always (uv-managed); never system
   python.
3. No `git commit --no-verify`; `ORI_SKIP_DOC_CHECK` is
   harness-DENIED — satisfy doc-coupling by pairing a
   behaviour change with the matching hand-written doc
   in the SAME commit.
4. `.docs_read_marker` refreshed before every commit via
   `echo "yes" | uv run python scripts/check_docs_read.py`.
5. Commit messages end the
   `Co-Authored-By: Claude Opus 4.7 (1M context)
   <noreply@anthropic.com>` trailer.
6. `_defaults.py` is the SOLE `uuid`/`datetime.now`
   binding site (`_owner_default.py` env-read exception);
   AST-pin every phase-11 NEW module; no vendor SDK at
   module load (Protocol DI).
7. Sources are READ-ONLY (§5.3.6); verbatim byte
   preservation (§5.3.7).
8. plan ≡ design ≡ tag annotation — one reconciliation
   pass at closeout; no silent design edit in a non-fix
   commit (the §0 amendment lands only after reviewer
   approval, in a slice-fix).
9. Leave `app/tools/youtube.py` dirty + `.playwright-mcp/`
   + `scripts/{amazon_ads_mcp_proxy,diag_gemini_caching,
   diag_removal_order}.py` untracked — intentional.
10. No sed; caveman full; Pacific tz; no time estimates.

— End of phase 11 plan —
