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

> **Round-1 CLOSED (claude-reviewer; read §0 + §9).**
> Both §12-step-11 templates have a HARD forward
> dependency on a subsystem that is NOT built and is NOT
> cleanly owned by step 11: `ChannelDigest(…,
> summary_prompt)` needs an LLM `ReasoningStep` chain
> executor (none in `app/v2/runtime/`; step 12 is
> reasoning *enforcement*, presupposing the executor),
> and stateful `RecurringSeriesFromSource(…,
> progress_strategy)` needs cross-fire `schedule_state`
> (§12 **step 13**, NOT built). Reviewer DECISION:
> phase 11 = the dependency-clean **fire-path cutover +
> emit-only source path + STATELESS
> `RecurringSeriesFromSource`** (11A); `ChannelDigest` +
> stateful progress defer to 11B. The round-1 🔴
> (`skip_unchanged` must not race the FRESH snapshot
> write) is fixed by the §0.1 additive
> `ResolveOutcome.changed_vs_prior` field. Full
> disposition: §9.

---

## 0. §12 step-11 refinement (CLOSED — claude-reviewer round 1 APPROVED the split)

Mirrors the phase-10 amendment discipline: the plan
records the refinement; `docs/CONTRACTS_V2_DESIGN.md` is
amended ONLY at closeout in the same reconciliation pass
(plan≡design load-bearing — no silent design edit in a
non-fix commit).

**APPROVED (round 1, Q1):** step 11 ships in two parts;
phase 11 = part A. Part B defers to a later phase that
depends on the reasoning executor + step 13 — a
non-summarising "digest" is a degraded product the user
did not ask for, and a stateful series without
`schedule_state` is unsafe (no CAS, lost-progress on
retry).

- **11A (this phase):** fire-path cutover; emit-only
  source delivery; `RecurringSeriesFromSource` with
  STATELESS progress strategies only (Q2 ACCEPTED).
- **11B (deferred):** `ChannelDigest` + `summary_prompt`
  (needs reasoning executor); stateful
  `RecurringSeriesFromSource` progress (needs step 13
  cross-fire state).

Closeout slice amends `docs/CONTRACTS_V2_DESIGN.md` §12
(step 11 → 11A/11B) so design ≡ plan ≡ shipped code.

### 0.1 Additive phase-10 contract change (CLOSED — round-1 🔴 fix)

`skip_unchanged` MUST NOT be realised by the worker
re-reading the prior snapshot: `resolve_source_cached`
writes the FRESH snapshot (`cache.py:253`) and the
resolver reads the prior BEFORE that cached call
(`resolver.py:223`). A worker-side
`_newest_materialised_snapshot` call AFTER resolve would
race the just-written FRESH row and is the round-1 🔴.

**Fix (additive, reviewer-approved):** `ResolveOutcome`
(phase-10-FROZEN `app/v2/sources/resolver.py`) gains
ONE additive field:

```
changed_vs_prior: Optional[bool] = None
```

defaulted → every existing phase-10 `ResolveOutcome`
construction / consumer is byte-unaffected. The resolver
populates it on the terminal RESOLVED / DRIFT outcomes
from the signal it ALREADY computes internally
(`shape_changed`, `resolver.py:271`) extended across
provenance (§3.3 table). (Exact line re-verified at
slice 4 against the then-current `resolver.py`.) The worker reads
`outcome.changed_vs_prior` ONLY — it NEVER calls
`_newest_materialised_snapshot` or `write_snapshot`
(Q5: resolver/cache OWNS the snapshot; worker is a PURE
consumer).

This is an explicit edit to a phase-10-frozen module;
declared here per the no-silent-frozen-edit rule. Slice 4
ships **phase-10 contract-regression pins**: after the
field add the resolver STILL (a) returns exactly one
terminal `ResolveOutcome`, (b) never raises into the
caller, (c) keeps the §3.5 typed-error taxonomy +
`fallback_eligible` intact, (d) leaves every prior
`ResolveOutcome` consumer unaffected (defaulted field).
`cache.py` is NOT touched (the resolver owns the prior↔
fresh comparison; the field only EXPOSES the
already-computed signal). Design §5.3.3/§5.3.5
live-change wording is reconciled at closeout (same pass).

---

## 1. Scope statement

### In scope (phase 11 / 11A)

1. **Worker fire-path cutover.** `Worker._dispatch_emit_branch`
   (`app/v2/runtime/worker.py`) stops raising
   `UnsupportedSpecError` for a source-driven spec. A
   source-driven `ScheduleSpec` =
   `execution_plan_hash` set → an `ExecutionPlan` whose
   `inputs` carry `InputSpec.source_ref` and whose
   `emits` are emit-only. The worker loads the plan
   (`get_execution_plan(conn, hash)` — failure modes
   enumerated in §3.1), resolves every source-bearing
   input, then runs the emit step(s). A plan with a
   NON-empty `reasoning` list is the step-12 boundary:
   the worker routes it through
   `_fail_run(reason="reasoning_unsupported_pending_step_12")`
   (Q4 — a clean `run_failed`, NOT a raise; the raise /
   `UnsupportedSpecError` pattern is reserved STRICTLY
   for the can't-write-a-failed-event cases, e.g.
   `schedule_not_found_at_claim` where the events-table
   FK makes the failure event unwritable).
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
   `progress_strategy`) — STATELESS strategies only
   (Q2): `whole` (emit the full resolved content each
   fire) and `skip_unchanged` (emit UNLESS
   `outcome.changed_vs_prior is False`). `skip_unchanged`
   reads the additive `ResolveOutcome.changed_vs_prior`
   (§0.1) — it NEVER re-reads the snapshot table from
   the worker (that races the FRESH write — the round-1
   🔴). NO new state table. Builder + typed args model
   mirror `build_one_off_reminder` / `OneOffReminderArgs`.
   Authoring tool mirrors `make_schedule_create_reminder`;
   it compiles a `ScheduleSpec` + `ExecutionPlan`
   (`inputs=[source_ref]`, `emits=[source_post]`,
   `reasoning=[]`).
5. **Failure routing.** Q6: EXTRACT a shared atomic
   single-transaction core (the round-3-hardened
   2–3-events + Run-UPDATE-in-ONE-`transaction(conn)`
   invariant, `worker.py:556-567`). The existing
   `_route_failure_policy(result: SlackPostResult)`
   call-site + behaviour stay BYTE-IDENTICAL (it just
   delegates to the extracted core); a sibling
   `_route_source_failure_policy` handles the typed
   resolve/emit failure via the same core. The
   single-transaction atomicity test is pinned for BOTH
   paths. `retry_later` stays downgraded to
   `alert_admin` + WARNING (retry chain = step 14,
   mirrors the phase-9 decision).
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
  a non-empty `reasoning` list → `_fail_run(reason=
  "reasoning_unsupported_pending_step_12")` (Q4 — a
  clean `run_failed`, NOT a raise; see §1.1 / §3.1).
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
(`_dispatch_emit_branch` source-driven branch + the Q6
`_route_*_failure_policy` extraction; slice 3 adds an
ADDITIVE optional `Worker(..., repo_root=None)` DI kwarg —
defaults to the prod repo root, threaded to
`resolve_source` so the per-fire snapshot lands under a
tmp tree in tests, never the working copy; existing
`Worker()` call sites are behaviourally unchanged),
`app/v2/sources/resolver.py` (phase-10-FROZEN — ONE
additive `ResolveOutcome.changed_vs_prior` field per
§0.1, reviewer-approved, with phase-10 contract-
regression pins), `app/v2/authoring/templates.py`
(`make_schedule_create_recurring_series_from_source`),
`app/sub_agents/coordinator_agent.py` (additive template
list + §11.4 scheduling-law), `scripts/check_phase_scope.py`
(`PHASE_ALLOWLIST[11]`), `.v2-current-phase`,
`docs/CONTRACTS_V2_DESIGN.md` (§12 step-11 → 11A/11B +
§5.3.3/§5.3.5 live-change wording — amended at CLOSEOUT
in the one reconciliation pass, never in a non-fix
commit; §0).

---

## 3. Module APIs (sketch — finalised per slice)

### 3.1 Worker source-driven branch (`worker.py`)

In `_dispatch_emit_branch`, the current
`if spec.execution_plan_hash is not None: raise
UnsupportedSpecError(...)` (worker.py:454) is REPLACED by
the source-driven branch (placed after the existing
deleted/inactive/staleness checks; the OneOff branch
below is untouched):

```
if spec.execution_plan_hash is not None:
    plan = get_execution_plan(conn, spec.execution_plan_hash)
    # get_execution_plan failure modes (enumerated):
    #  - None (no row for the hash)
    #       -> _fail_run(reason="execution_plan_missing_at_claim"); return "failed"
    #  - row present, body_json fails ExecutionPlan(**body) validation
    #       -> _fail_run(reason="execution_plan_invalid_at_claim"); return "failed"
    #  - sqlite OperationalError / DB-locked / corruption
    #       -> propagates to run_loop (transient infra; Run stays
    #          RUNNING; boot recovery promotes — SAME as the
    #          phase-9 transient path; NOT a _fail_run, NOT a raise
    #          we introduce). The events-table FK is intact here
    #          (schedule row present) so this is not a
    #          can't-write-failed-event case.
    if plan is None:
        await self._fail_run(conn=conn, run=run,
            reason="execution_plan_missing_at_claim",
            error_message=...); return "failed"
    if plan.reasoning:                       # step-12 boundary (Q4)
        await self._fail_run(conn=conn, run=run,
            reason="reasoning_unsupported_pending_step_12",
            error_message=...); return "failed"

    resolved: dict[str, ResolveOutcome] = {}
    for inp in plan.inputs:
        if inp.source_ref is None:
            continue
        outcome = await resolve_source(
            ref=inp.source_ref,
            source_id=inp.id,            # STABILITY PIN: source_id == InputSpec.id
            schedule_id=spec.id,
            run_id=run.id,
            conn=conn,
            conn_factory=self._conn_factory,
            clock=self._clock,
            event_id_factory=self._event_id_factory,
            as_of_datetime=None,         # live fire (not a dry-run)
            audit=spec.audit,
            # loaders / repo_root default to the prod singletons
        )
        if outcome.status is ResolveStatus.FAILED:
            await self._route_source_failure_policy(
                conn=conn, run=run, spec=spec, outcome=outcome)
            return "failed"
        resolved[inp.id] = outcome       # RESOLVED or DRIFT(alert serves)

    result = await emit_source_to_slack(
        spec=spec, plan=plan, resolved=resolved,
        slack_client=self._slack_client, clock=self._clock,
    )
    if result.skipped_unchanged:         # skip_unchanged &&
        return "succeeded"               #   outcome.changed_vs_prior is False
                                         # (emit_skipped_unchanged event written
                                         #  in-band by the emit step)
    if result.ok:
        return "succeeded"
    await self._route_source_failure_policy(
        conn=conn, run=run, spec=spec, emit_result=result)
    return "failed"
```

Required points:
- **kw-args** to `resolve_source` are EXACTLY the
  phase-10 signature: `ref, source_id, schedule_id,
  run_id, conn, conn_factory, clock, event_id_factory,
  as_of_datetime, audit` (+ defaulted `loaders`,
  `repo_root`). Called with the worker's DI
  clock / `event_id_factory` / `conn_factory` — NEVER
  `datetime.now` / `uuid4`.
- **`source_id == inp.id` stability pin.** The resolver
  keys the per-fire snapshot by `source_id`; `InputSpec.id`
  is the stable snake_case (`^[a-z][a-z0-9_]*$`)
  identifier frozen in the ExecutionPlan body, so the
  snapshot identity is stable across fires/retries. A
  test asserts `resolve_source` is called with
  `source_id == inp.id` and that a re-fire reuses the
  same snapshot key.
- **Exactly-one-terminal-outcome / never-raise** ⇒ the
  worker has NO `try/except` around `resolve_source` for
  control flow; it switches on `outcome.status` only.
- **Q5: pure consumer.** The worker NEVER calls
  `write_snapshot` NOR `_newest_materialised_snapshot`.
  `skip_unchanged` reads `outcome.changed_vs_prior`
  (§0.1) only.

### 3.2 `emit_source_to_slack` (`emit/source_post.py`)

Mirrors `emit_reminder_to_slack`:
`async emit_source_to_slack(*, spec, plan, resolved:
dict[str, ResolveOutcome], slack_client: SlackProtocol,
clock) -> SourcePostResult`.
`SourcePostResult(ok: bool, channel: str, ts: Optional[str],
error: Optional[str], skipped_unchanged: bool = False)`.
Channel + `progress_strategy` come from the `source_post`
EmitStep `args` compiled by the template. The emit step
reads `resolved[input_id].changed_vs_prior` (§0.1) — it
NEVER touches the snapshot table. Under
`progress_strategy="skip_unchanged"`, if
`changed_vs_prior is False` for the (single phase-11)
source input → NO Slack call, `skipped_unchanged=True`,
and the step writes an `emit_skipped_unchanged` event
(in-band, same transaction discipline as a normal emit).
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
`skip_unchanged` is realised from
`ResolveOutcome.changed_vs_prior` (§0.1) ONLY — the
worker/emit step NEVER re-reads the snapshot table
(that races the FRESH write — round-1 🔴). The resolver
populates `changed_vs_prior` per source provenance
(all-provenance semantics — round-1 🟡):

| provenance | `changed_vs_prior` | skip_unchanged |
|---|---|---|
| `FRESH` (prior exists) | `content_hash != prior_hash` | skip iff hashes equal |
| `FRESH` (no prior — first fire) | `True` | emit (first fire always emits) |
| `CACHE_HIT` (within no-probe TTL) | `False` | skip (snapshot IS the source; no new content) |
| `FALLBACK_LAST_GOOD` | `False` | skip (literally re-serving last-good) |
| `FALLBACK_DEFAULT` | `None` | EMIT (a degraded default MUST surface — skip_unchanged NEVER suppresses a fallback-to-default; AI_EDITS rule 13) |

Worker rule: under `skip_unchanged`, EMIT unless
`changed_vs_prior is False` (so `None` ⇒ emit). Under
`whole`, ALWAYS emit regardless of `changed_vs_prior`.
NO new state table. The phase-10 contract-regression
pins (§0.1 / §5) prove the field add did not break
exactly-one-outcome / never-raise / §3.5.

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
push mid-phase. Cadence mirrors phases 9/10. **9 working
slices** (Q7: old slice 4 split into 4 + 6 — the 🔴
contract change is front-loaded as its own slice with
the phase-10 regression pins; failure-policy extraction
moved BEFORE the resolve-wire so a FAILED resolve has a
landing path — round-1 🟡 slice-2/6 reorder).

0. **plan + phase transition** (this commit; NOT pushed)
   — `docs/PHASE_11_PLAN.md`, `PHASE_ALLOWLIST[11]` in
   `scripts/check_phase_scope.py`, `.v2-current-phase`→11.
   The transition flip MUST be in this commit so the
   scope guard accepts the plan commit's own diff
   (phase-10 cadence). Design doc NOT touched here.
1. **Worker plan-load + source-driven routing** — load
   `ExecutionPlan`; route inputs+emits-only plans into
   the new branch; `_fail_run(reason=
   "reasoning_unsupported_pending_step_12")` for
   reasoning-bearing plans (Q4 — NOT a raise);
   `_fail_run("execution_plan_missing_at_claim")` /
   `"execution_plan_invalid_at_claim"`; enumerate the
   `get_execution_plan` failure modes (§3.1). Pin
   Q5 (worker NEVER calls `write_snapshot` /
   `_newest_materialised_snapshot`). NO resolve / NO
   emit yet — the branch ends in a deterministic
   `_fail_run("source_fire_not_yet_wired")` so nothing
   fires; purely the routing skeleton + tests.
2. **Failure-policy extraction (Q6)** — extract the
   round-3-hardened single-`transaction(conn)` core
   (2–3 events + Run UPDATE atomic, `worker.py:556-567`);
   `_route_failure_policy(result: SlackPostResult)`
   call-site + behaviour stay BYTE-IDENTICAL (delegates
   to the core); add the sibling
   `_route_source_failure_policy` for the typed
   resolve/emit failure. Atomicity test pinned for BOTH
   paths. (Moved ahead of the resolve-wire so slice 3's
   FAILED branch has its destination — round-1 🟡.)
3. **`resolve_source` wired in** — call the resolver per
   source-bearing input on the claimed conn with the
   exact §3.1 kw-args (`source_id == inp.id` pin); map
   `ResolveOutcome` → continue / fail; honour
   exactly-one-outcome / never-raise (no control-flow
   `try/except`); FAILED & `require_reapprove` →
   `_route_source_failure_policy`; DRIFT(alert) → carry.
   No emit yet — after all inputs resolve the worker
   stops DETERMINISTICALLY via
   `_fail_run(reason="source_resolved_no_emit")` (NOT a
   success: a source-driven schedule must never report
   success without delivering, and must never partially
   fire). That reason is the slice-3 test hook — the
   resolver's `SOURCE_RESOLVED`/`SOURCE_DRIFT_DETECTED`
   events prove resolve ran end to end while emit is
   cleanly deferred to slice 5. Worker gains an additive
   `repo_root` DI (snapshot audit root; tmp in tests).
4. **Phase-10 additive contract change (the 🔴,
   front-loaded — Q7 "4a").**
   `ResolveOutcome.changed_vs_prior: Optional[bool] =
   None` (§0.1); resolver populates it on RESOLVED /
   DRIFT per the all-provenance table (§3.3). Ships the
   **phase-10 contract-regression pins**: resolver still
   (a) exactly one terminal outcome, (b) never raises,
   (c) §3.5 taxonomy + `fallback_eligible` intact, (d)
   every prior `ResolveOutcome` consumer unaffected
   (defaulted). `cache.py` untouched. NO worker use yet.
5. **`emit/source_post.py`** — `SourcePostResult`
   (incl. `skipped_unchanged`) + `emit_source_to_slack`;
   wire resolved bytes → Slack VERBATIM; `OneOffReminder`
   path byte-untouched; full resolve→emit→succeeded path
   live for `progress_strategy="whole"`.
6. **`RecurringSeriesFromSource` builder + skip_unchanged
   (Q7 "4b"; depends 4 + 5).**
   `templates/recurring_series_from_source.py`; typed
   args; `whole` + `skip_unchanged`. `skip_unchanged`
   reads `outcome.changed_vs_prior` ONLY (NEVER the
   snapshot table); `skipped_unchanged` no-op-success
   path + `emit_skipped_unchanged` event; all-provenance
   table (§3.3) pinned.
7. **Authoring tool** —
   `make_schedule_create_recurring_series_from_source`;
   ExecutionPlan compile (inputs+emit, zero reasoning);
   validation / dry-run / freeze / commit end-to-end;
   LLM-visible-signature DI-leak pin.
8. **Failure-policy integration + agent guidance +
   hygiene** — end-to-end FAILED / `require_reapprove` /
   DRIFT pins through `_route_source_failure_policy`;
   `retry_later` → `alert_admin` + WARNING (step-14
   note); additive coordinator §11.4 scheduling-law
   (template list + `RecurringSeriesFromSource`);
   `test_phase11_import_hygiene.py` AST pin.
   (`PHASE_ALLOWLIST[11]` / `.v2-current-phase` already
   flipped in slice 0; only EXTEND the allowlist if a
   new surface path appeared.)
9. **closeout** — full `tests/v2`, §7 acceptance walk,
   phase guards, `gen_docs` regen+stage, annotated tag
   `v2-phase-11-complete` (gated on reviewer CLOSEOUT
   PASS), and the ONE reconciliation pass amending
   `docs/CONTRACTS_V2_DESIGN.md` §12 (step 11 → 11A/11B)
   + §5.3.3/§5.3.5 live-change wording so design ≡ plan
   ≡ shipped code.

---

## 5. Test inventory (highlights)

- `test_runtime_source_fire.py` — source-driven spec
  fires end to end (RESOLVED→emit→succeeded); the exact
  `get_execution_plan` failure-mode set: `None` →
  `_fail_run("execution_plan_missing_at_claim")`,
  invalid body → `_fail_run("execution_plan_invalid_at_claim")`,
  sqlite-transient → propagates (Run stays RUNNING,
  recovery promotes — NOT a `_fail_run`/raise we add);
  reasoning-bearing plan →
  `_fail_run("reasoning_unsupported_pending_step_12")`
  (Q4 — a `run_failed`, NOT a raise); resolver FAILED →
  `run_failed` via `_route_source_failure_policy`, NO
  emit, NO partial; `require_reapprove` → withhold +
  FAILED + snapshot still written by the resolver;
  DRIFT(alert) → drift event + STILL emits; resolver
  called with DI clock/id-factory and **`source_id ==
  inp.id`** (stability pin — and a re-fire reuses the
  same snapshot key); resolver never-raise honoured (a
  forced internal resolver fault → single SOURCE_FAILED
  outcome, worker `_fail_run`s cleanly, no crash, no
  control-flow `try/except`).
- `test_resolver_phase10_contract_regression.py`
  (slice 4, §0.1) — after the `changed_vs_prior` field
  add the resolver STILL: returns exactly ONE terminal
  `ResolveOutcome`; never raises into the caller;
  preserves the §3.5 typed-error taxonomy +
  `fallback_eligible` (only `SourceFetchError`); every
  pre-existing `ResolveOutcome` construction/consumer is
  byte-unaffected (defaulted field). `changed_vs_prior`
  populated correctly for ALL provenances per the §3.3
  table (FRESH±prior / CACHE_HIT / FALLBACK_LAST_GOOD /
  FALLBACK_DEFAULT).
- `test_failure_policy_atomicity.py` (slice 2) — the
  single-`transaction(conn)` invariant holds for BOTH
  `_route_failure_policy` (SlackPostResult — behaviour
  BYTE-IDENTICAL to phase-9: a raise mid-routing leaves
  NO partial events + Run not stuck `running`) AND
  `_route_source_failure_policy` (typed resolve/emit
  failure — same atomic core).
- `test_emit_source_post.py` — `emit_source_to_slack`
  ok / not-ok; verbatim bytes preserved (§5.3.7);
  channel from EmitStep args; `skip_unchanged` +
  `changed_vs_prior is False` → NO Slack call +
  `skipped_unchanged=True` + `emit_skipped_unchanged`
  event; `OneOffReminder` emit byte-unaffected.
- `test_templates_recurring_series_from_source.py` —
  builder produces a valid `ScheduleSpec`+`ExecutionPlan`
  (zero reasoning, one source input, one source_post
  emit); `whole` always emits; `skip_unchanged` decision
  driven by `changed_vs_prior` across the FULL §3.3
  provenance table (incl. FALLBACK_DEFAULT → EMIT);
  naive datetime / bad slot rejected at the builder.
- `test_runtime_worker_emit_branch.py` (extend) — the
  source-driven branch coexists with the OneOff branch;
  the phase-9 `schedule_not_found` / inactive / template
  invariants still hold; the worker NEVER calls
  `write_snapshot` / `_newest_materialised_snapshot`
  (Q5 — assert via patch/spy).
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
3. The worker calls `resolve_source` with the exact
   phase-10 kw-args, DI clock/id-factory, and
   `source_id == inp.id` (stability pin; re-fire reuses
   the snapshot key). The phase-10 resolver contract is
   unbroken AFTER the `changed_vs_prior` field add
   (§0.1): exactly one terminal outcome; never raises
   into the worker (forced-fault pin); §3.5 taxonomy +
   `fallback_eligible` intact; every prior
   `ResolveOutcome` consumer byte-unaffected. The worker
   has NO control-flow `try/except` around the resolver.
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
   produce a valid spec; `whole` always emits;
   `skip_unchanged` is driven by
   `ResolveOutcome.changed_vs_prior` across the FULL §3.3
   provenance table (FRESH±prior / CACHE_HIT /
   FALLBACK_LAST_GOOD skip; FALLBACK_DEFAULT EMITS); the
   worker NEVER re-reads the snapshot table
   (`_newest_materialised_snapshot` / `write_snapshot`
   never called — Q5); NO new state table introduced.
8. The step-12 boundary uses `_fail_run` (Q4), NOT a
   raise: a reasoning-bearing ExecutionPlan →
   `_fail_run(reason="reasoning_unsupported_pending_step_12")`;
   missing plan → `_fail_run("execution_plan_missing_at_claim")`;
   invalid plan body →
   `_fail_run("execution_plan_invalid_at_claim")`. The
   raise / `UnsupportedSpecError` pattern is reserved
   STRICTLY for the can't-write-a-failed-event cases
   (`schedule_not_found_at_claim`, FK-unwritable).
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
skip_unchanged). skip_unchanged reads an ADDITIVE
ResolveOutcome.changed_vs_prior signal the resolver
populates per source provenance (FRESH / CACHE_HIT /
FALLBACK_LAST_GOOD / FALLBACK_DEFAULT); the worker is a
pure snapshot consumer (never re-reads the snapshot
table). NO new state table. The reasoning-bearing-plan
step-12 boundary is a clean run_failed
(_fail_run reason=reasoning_unsupported_pending_step_12),
NOT a raise; the raise pattern stays reserved for the
can't-write-a-failed-event cases.

NOT shipped (deferred): ChannelDigest + summary_prompt
(needs the LLM reasoning-chain executor); stateful
RecurringSeriesFromSource progress (needs step-13
cross-fire schedule_state); retry chain (step 14);
v1->v2 migration tooling (step 16). §12 step 11 is split
into 11A (this phase) + 11B (deferred) per the
reviewer-approved §0 refinement.

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 11, §5.2,
        §5.3, §5.3.3, §5.3.5, §11.1/§11.4
Plan:   docs/PHASE_11_PLAN.md
```

---

## 9. Codex/claude-reviewer round-1 disposition (CLOSED)

All 7 questions adjudicated by claude-reviewer in
plan-review round 1; decisions baked into §0–§7 of this
revision. Recorded here verbatim so a future drift is
caught against the decision, not re-litigated (the
phase-10 disposition-log discipline).

- **Q1 — `ChannelDigest`.** DECISION = (a): defer
  `ChannelDigest` entirely to **11B** (post reasoning
  executor). No scope growth. Baked: §0, §1 out-of-scope,
  §8.
- **Q2 — progress strategies.** DECISION = ACCEPT
  stateless-only (`whole` + `skip_unchanged`); stateful
  "next unread item" defers to 11B (step-13 cross-fire
  `schedule_state`). Baked: §0, §1.4, §3.3.
- **Q3 — emit adapter.** DECISION = NEW
  `app/v2/emit/source_post.py`; `OneOffReminder` /
  `emit_reminder_to_slack` stay byte-untouched (additive
  cutover). Baked: §1.3, §2, §3.2, slice 5.
- **Q4 — reasoning-bearing plan.** DECISION =
  `_fail_run(reason="reasoning_unsupported_pending_step_12")`
  — a clean `run_failed`, NOT a raise /
  `UnsupportedSpecError`. The raise pattern is reserved
  STRICTLY for the can't-write-a-failed-event cases
  (FK-unwritable, e.g. `schedule_not_found_at_claim`).
  This also resolves the §3.1 internal inconsistency
  (now symmetric with the plan-missing path). Baked:
  §1.1, §1 out-of-scope, §3.1, §7-#8, §9-Q4.
- **Q5 — snapshot ownership.** CONFIRMED: the
  resolver/cache OWNS the per-fire snapshot write
  (`resolve_source_cached` writes on FRESH at
  `cache.py:253`; the resolver reads the prior BEFORE
  that cached call at `resolver.py:223`). The worker is
  a PURE consumer — it MUST NOT call `write_snapshot`
  AND MUST NOT call `_newest_materialised_snapshot`
  (re-reading it for `skip_unchanged` races the FRESH
  write — this is the round-1 🔴, fixed via §0.1's
  additive `changed_vs_prior`). Slice-1 pin is correct +
  necessary. Baked: §0.1, §3.1, §3.3, slice 1, §5.
- **Q6 — failure-policy reuse.** DECISION = neither
  pure-generalise nor pure-duplicate: EXTRACT the shared
  atomic single-`transaction(conn)` core (the
  round-3-hardened 2–3-events + Run-UPDATE invariant,
  `worker.py:556-567`); keep
  `_route_failure_policy(result: SlackPostResult)`
  call-site/behaviour BYTE-IDENTICAL (delegates to the
  core); add sibling `_route_source_failure_policy` for
  the typed resolve/emit failure; pin the atomicity
  test for BOTH paths. Baked: §1.5, §3.1, slice 2, §5.
- **Q7 — slice count.** DECISION = split into **9**
  (old slice 4 → slice 4 "resolver-signal contract
  change + phase-10 regression pins" + slice 6 "template
  + skip_unchanged emit"); the 🔴 contract change is its
  own front-loaded slice; the failure-policy extraction
  moves ahead of the resolve-wire (round-1 🟡
  slice-2/6 reorder). Baked: §4.

### Round-1 must-fix ledger (every item applied)

- 🔴 `skip_unchanged` no longer re-reads the snapshot
  table; uses the additive
  `ResolveOutcome.changed_vs_prior` (§0.1) — declared as
  an explicit additive edit to phase-10-frozen
  `resolver.py` WITH phase-10 contract-regression pins.
- 🟡 all-provenance `skip_unchanged` semantics specified
  (§3.3 table: FRESH±prior / CACHE_HIT /
  FALLBACK_LAST_GOOD / FALLBACK_DEFAULT).
- 🟡 slice-2/slice-6 reorder applied (§4: failure-policy
  extraction is now slice 2, before the resolve-wire).
- 🟡 `get_execution_plan` failure modes enumerated
  (§3.1: None / invalid-body / sqlite-transient).
- 🟡 Q4 `_fail_run` decision applied consistently across
  §1 / §3.1 / §7-#8 / §9-Q4.
- §3.1 resketched with the exact phase-10 kw-args + the
  `source_id == inp.id` stability pin.

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
