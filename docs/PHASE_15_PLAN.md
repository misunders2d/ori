# PHASE 15 PLAN — §12 step 15: Observability

> Relay: slice-gated writer↔claude-reviewer via CONDUCTOR.
> All-remaining directive — iterate every remaining v2 phase
> (`docs/CONTRACTS_V2_DESIGN.md` §12) until the plan is
> complete OR human intervention. Plan-review round 1 next.

§12 step 15 canonical (design line 1652): **Observability**:
`schedule_status`, `schedule_diff`, `schedule_replay`,
background failure monitor over EventLedger. §9 enumerates
the primitive set.

## 0. Scope refinement + open design questions (round-1 input)

### 0.0 Verified code-surface premises (pre-arm item 1 — done FIRST)

Every phase in this relay had ≥1 code-busted premise
(phase-14 slice-3 spec-JSON-in-schedules; 11/12/13 forks).
The phase-15 premises were grep-verified against the ACTUAL
surface BEFORE drafting:

- **No observability primitive exists.** `grep` for
  `schedule_status|schedule_health|schedule_diff|schedule_history|schedule_failures|registry_status|schedule_replay`
  in `app/v2/` returns only incidental doc/comment mentions —
  NO implemented tool/module. Phase 15 BUILDS them.
- **No metrics/trace sink exists.** `Protocol` classes in
  `app/v2/` are domain loaders (`SourceLoader`,
  `SlackProtocol`, `Migration`, registry clients) — NO
  `MetricsSink`/`TraceSink`/exporter. An external exporter is
  NOT in the canonical §9 list (Q6).
- **The read substrate is live + pure-read, already shipped.**
  `storage/events.py` (`list_events_for_run`,
  `list_events_for_schedule`, `get_last_emit_succeeded`,
  `append_event`), `storage/runs.py` (`get_run`,
  `list_runs_in_chain`, `list_pending_due`,
  `list_claimable_due`, `mark_run_status`),
  `storage/schedules.py` (`get_schedule`,
  `list_active_schedules`). The EventLedger is written every
  fire by the phase-9–14 cores. Observability REUSES this —
  NO SQL re-implementation (the phase-14 Q6 discipline).
- **Return shape exists**: `authoring/responses.py`
  `ToolResponse` (`ok` / `validation_failed` / `not_found`).
- **Transition surface**: `.v2-current-phase` = `14`;
  `PHASE_ALLOWLIST` keyed by int, `[13] == [14]` full-set
  structure (`scripts/check_phase_scope.py:300` / `:328`).

### 0.1 Classification — LIVE substrate + pure-read side-channel (pre-arm item 2, the recurring Q1)

Observability is NOT phases-11–13 build-the-layer-DEAD (the
EventLedger substrate IS live — every fire writes events) and
NOT a fire-path mutation (every primitive is a pure read).
It is a **pure read-only side-channel over an already-live
substrate**: ZERO fire-path control flow, no event the
dedup / terminal / failure cores do not already write, no new
EventKind, no v002. The ONLY periodic-loop piece is the
background failure monitor — sliced last, build-the-layer
(pure detector query; the side-effecting re-alert dispatch +
binding wiring DEFERRED, Q2). Additive-instrumentation
discipline is therefore airtight-by-construction and pinned
(§11.1 byte-proof + phase-9–14 boundary regression pins on
the live slice).

### 0.2 Open design questions for round 1 (verify-first, lowest-risk, bind)

- **Q1 — classification.** RECOMMENDED: pure-read query /
  aggregation primitives (build the query layer, NO agent
  mount); the failure monitor is a build-the-layer pure
  detector FUNCTION (DI-conn / DI-clock), NOT wired into the
  APScheduler binding in phase 15 (mirrors phases 10/12/13
  build-the-layer; the periodic wiring is a later step). NOT
  a live fire-path consumer. Reviewer ratifies live-vs-BTL.
- **Q2 — failure-monitor re-alert mechanism.** Design §9:
  `SELECT … admin_alert_sent NOT IN (… admin_alert_acked) …
  Re-alert.` RECOMMENDED: ship the PURE detector query only
  (returns the unacked-overdue set); the side-effecting
  re-alert DM/event-emit is DEFERRED (build-the-layer). If a
  re-alert is ever emitted it REUSES `admin_alert_sent` — NO
  new EventKind, NO v002 — unless an explicit verify-first
  fork ratifies otherwise (the phase-11-Option-B /
  phase-14-(α) discipline).
- **Q3 — primitive set for phase 15.** §9 lists 7.
  RECOMMENDED IN: the pure-read aggregations —
  `schedule_status`, `schedule_failures`, `schedule_history`,
  `schedule_health`, `registry_status`, `schedule_diff`.
  RECOMMENDED DEFER: `schedule_replay` (re-runs a past Run in
  dry-run mode — touches the dry-run / fire path + stored
  snapshots; NOT pure-read; its own slice or a later step).
  Reviewer rules the cut. **[SUPERSEDED by §9.1 (slice-1
  fork ruling α): `schedule_diff` is NOT a pure-read
  aggregation — code-verified there is NO persisted
  historical ScheduleSpec body to diff (`schedules`
  one-row-per-id current-only; `schedule_created`
  payload={hash,template}; no `schedule_revised` emitter;
  `execution_plans`=ExecutionPlan-not-ScheduleSpec bodies).
  DEFERRED exactly as `schedule_replay`. IN-set amended
  6→5: `schedule_status`, `schedule_failures`,
  `schedule_history`, `schedule_health`, `registry_status`.]**
- **Q4 — return shape.** RECOMMENDED: typed Pydantic result
  models (the §3.5 typed-result discipline) returned via
  `ToolResponse.ok` payload; no loose dicts. Verify
  `ToolResponse` carries an arbitrary typed payload or add a
  dedicated observability response model.
- **Q5 — mount.** RECOMMENDED: NO new agent mount in phase 15
  (the phases-10/12/13/14 relay pattern — authored surface /
  build-the-layer only). `PHASE_ALLOWLIST[15]` mirrors
  `[13]`/`[14]` (no agent surface, no boot integration).
- **Q6 — external metrics/trace exporter (statsd / OTel).**
  NOT in the canonical §9 list (§9 = ledger-read primitives +
  failure monitor). RECOMMENDED: OUT of phase-15 scope
  (deferred). If ever ratified it MUST be a `Protocol` / DI
  seam with NO vendor-SDK at module load (the alias-robust
  AST-pin discipline carried 11/12/13/14). Surfaced — not
  assumed in/out.
- **Q7 — registry_status source.** `registry_status` →
  cached enums + staleness. Verify the phase-6 registry-cache
  read surface (`app/v2/registry_cache/`) exposes staleness;
  REUSE it (no re-implementation). Bind after code-check.

## 1. Scope statement

### In scope (phase 15), assuming Q1 BTL / Q2–Q7 RECOMMENDED

1. **Pure-read observability primitives** over the EventLedger
   + schedules read substrate — the **5** Q3 IN-set
   (amended 6→5 by §9.1): `schedule_status`,
   `schedule_failures` (per-schedule), `schedule_history`,
   `schedule_health` (DI-clock), `registry_status` (reuses
   phase-6 `registry_cache`). Dedicated typed Pydantic result
   models (Q4 — `ToolResponse` code-verified insufficient).
   NO SQL re-implementation — compose the shipped
   `storage/events.py` / `storage/schedules.py` /
   `registry_cache` pure-read API. ZERO fire-path touch.
2. **Background failure-monitor pure detector** — a
   build-the-layer function (DI-conn / DI-clock) returning
   the unacked-overdue `admin_alert_sent` set. NO periodic
   wiring, NO re-alert side-effect, NO new EventKind (Q1/Q2).
3. **Hygiene + docs.** Alias-robust AST import-hygiene pin
   for any new module. `PHASE_ALLOWLIST[15]` +
   `.v2-current-phase`→15 land in the plan commit (this
   commit). `gen_docs` regen + stage at closeout.

### Out of scope (phase 15) — deferred with rationale

- **`schedule_replay`** (Q3) — touches the dry-run / fire
  path + stored snapshots; not pure-read; its own slice or a
  later step.
- **`schedule_diff`** (slice-1 fork ruling α, §9.1) —
  code-verified NO persisted historical ScheduleSpec body to
  diff: `schedules` is one-row-per-id current-only,
  `schedule_created` payload is `{hash, template}` only, no
  `schedule_revised` emitter exists, `execution_plans` stores
  ExecutionPlan (not ScheduleSpec) bodies. A real §9
  `schedule_diff` (unified diff of two historical spec bodies
  + tool-tag snapshots) needs a spec-version store that is
  NOT shipped — its own future design + DDL decision
  (no-v002-relitigation), NOT a §15 pure-read primitive.
  Same deferral class as `schedule_replay`.
- **Failure-monitor periodic wiring + re-alert dispatch**
  (Q1/Q2) — side-effecting; build-the-layer detector only.
- **External metrics/trace exporter** (Q6) — not canonical
  §9; deferred; Protocol/DI if ever ratified.
- **Migration tooling** (§12 step 16); reasoning /
  stateful-flow executor (no §12 step owns it); the retry
  CHAIN (DEFERRED — no §12 step owns it, per phase-14
  closeout-fix).

## 2. New file paths (provisional — finalised per slice)

- `app/v2/observability/` (new package) — pure-read query /
  aggregation primitives + typed result models + the
  failure-monitor pure detector. Pure / DI-conn, no vendor
  SDK, no module-load nondeterminism.
- `tests/v2/test_observability_*.py` —
  per-primitive + the detector + a folded alias-robust
  AST import-hygiene pin for the new package.

## 3. Module APIs (sketch — finalised per slice)

- Each primitive: `def <name>(conn, *, …) -> <TypedResult>`
  — pure read, no mutation, composes the shipped `storage/*`
  read API (no SQL reimpl). Typed Pydantic result models.
- `failure_monitor_scan(conn, *, now, threshold) ->
  list[OverdueAlert]` — pure detector; NO emit, NO event
  write, NO binding wiring (build-the-layer).
- Returned to callers via `ToolResponse.ok(payload=…)` (Q4).

## 4. Slice ordering + commit cadence (draft — reviewer finalises)

0. **plan + phase transition** (this commit; NOT pushed) —
   `docs/PHASE_15_PLAN.md`, `PHASE_ALLOWLIST[15]`,
   `.v2-current-phase`→15. Design doc NOT touched here (the
   ONE reconciliation pass is closeout).
1. **Pure-read primitives + typed result models** (Q3 set) —
   decision / pure layer, NO wiring. Per-primitive tests +
   the folded hygiene pin. ZERO cross-phase touch (emit /
   sources / storage / ddl / worker EMPTY-diff vs
   `v2-phase-14-complete`).
   **LANDED** (slice-1 fork ruling α, §9.1; folded in-commit
   per the phase-11 §0.3 / phase-14 §9.2 disposition
   discipline). New `app/v2/observability/` package
   (`__init__.py`, `results.py`, `primitives.py`) — pure /
   DI-conn / DI-clock, no module-load
   `datetime.now` / `uuid4` / vendor-SDK. **5** primitives
   (Q3 amended 6→5; `schedule_diff` DEFERRED, §9.1):
   `schedule_status` (get_schedule + `list_events_for_schedule`
   → recent-run summaries + pause/archive + unacked-alert
   count), `schedule_failures` (per-schedule — merges the 4
   failure kinds via `list_events_for_schedule(kind=…)`,
   most-recent-first, limit-guarded), `schedule_history`
   (chronological), `schedule_health` (DI-`now`,
   `fire_ok_rate` None — never silent 0.0 — when no terminal
   in window), `registry_status` (REUSES phase-6
   `load_cache` / `is_stale`, no DB, no re-impl). Dedicated
   typed Pydantic result model per primitive (Q4 —
   `ToolResponse` code-verified insufficient; NOT mutated).
   Composes ONLY the shipped `storage/events.py` /
   `storage/schedules.py` / `registry_cache` reads — NO SQL
   reimpl. `tests/v2/test_observability.py`: per-primitive
   correctness + a PURITY pin (events/runs/schedules/
   schedule_state full-table fingerprint byte-unchanged
   post-call) + the folded alias-robust AST import-hygiene
   pin. 2421 v2 tests, 0 fail, 0 regression (2410 phase-14
   baseline + 11). ZERO cross-phase touch verified:
   `emit/` `sources/` `storage/` `ddl/` `worker.py`
   `registry_cache/` all 0-diff vs `v2-phase-14-complete`.
   NO wiring / NO detector (slice 2). `PHASE_ALLOWLIST[15]`
   unchanged (the `app/v2/` prefix covers `observability/` —
   no genuinely new surface path).
2. **Failure-monitor pure detector** — build-the-layer
   function (DI-conn / DI-clock), NOT wired; reuses
   `admin_alert_sent` / `admin_alert_acked` (no new kind).
   Carries the §11.1 byte-proof + phase-9–14 boundary
   regression pins (the live-substrate slice).
   **LANDED** (slice-2; folded in-commit per the phase-11
   §0.3 / phase-14 §9.2 discipline). New
   `app/v2/observability/failure_monitor.py`:
   `failure_monitor_scan(conn, *, now, threshold) ->
   list[OverdueAlert]` + `OverdueAlert` typed model.
   Realises the design §9:1329 query — `admin_alert_sent`
   whose `id` ∉ `{admin_alert_acked.correlates}` AND
   `sent_ts < now - threshold`, oldest-first. **§9:1329 is
   an inherently GLOBAL cross-schedule sweep; the shipped
   read surface is per-run / per-schedule and NONE composes
   a global admin-alert query** — slice-2 may not add a
   storage read (§11.1 byte-proof), and per-schedule
   iteration would silently MISS unacked alerts on
   paused/archived schedules (a correctness bug + the
   phase-11-ChannelDigest-(b) honest-scope anti-pattern). So
   the detector issues the canonical §9:1329 query directly
   as a PURE read (SELECT only, ZERO mutation —
   PURITY-pinned); this is the detector's OWN canonical
   query, NOT a re-implementation of any shipped
   per-schedule helper (none exists for the global sweep —
   the slice-2 pre-arm "NO SQL reimpl IF a shipped read
   composes it" conditional explicitly anticipated this; NO
   FORK — no busted premise, the conditional resolved by
   code-check). DI-`now` (no module-load clock). NOT wired:
   NO periodic / APScheduler binding, NO re-alert dispatch —
   DEFERRED, closeout-recorded; reuses both v001-CHECK kinds
   (no new EventKind, no v002). A future re-alert needing
   otherwise = a verify-first fork. `tests/v2/test_observability.py`
   extended: unacked-overdue returned oldest-first +
   ack-clears-correlation + below-threshold-not-flagged + a
   PURITY pin (full-table fingerprint byte-unchanged); the
   package AST import-hygiene glob auto-covers the new
   module. 2425 v2 tests, 0 fail, 0 regression (2421
   slice-1 + 4 slice-2). §11.1 byte-proof: `emit/`
   `sources/` `cache.py` `resolver.py` `storage/` `ddl/`
   `worker.py` ALL 0-diff vs `v2-phase-14-complete`;
   phase-9–14 boundary regression pins (worker-reasoning-seam
   / validation-reasoning-tool-mode / idempotency-worker-dedup
   / paused-pending-policy / runtime-source-fire) UNMODIFIED
   + green. `PHASE_ALLOWLIST[15]` unchanged.
3. **closeout** — full `tests/v2`, §7 acceptance walk, phase
   guards `--staged` + `--diff v2-phase-14-complete`,
   `gen_docs` regen+stage, REPO-WIDE SEMANTIC-INTENT
   stale-wording sweep (the phase-9–14 lesson — MUST catch
   any `lands in §12 step 15` / forward-ref prior shipped
   phases now imply + any new false claim), the ONE
   `docs/CONTRACTS_V2_DESIGN.md` reconciliation pass (§9 LIVE
   notes: primitives shipped pure-read, failure-monitor
   detector build-the-layer, replay/exporter deferred),
   annotated tag `v2-phase-15-complete` (gated on a
   claude-reviewer CLOSEOUT PASS).

## 5. Test inventory (highlights)

- Per primitive: correctness over a seeded ledger; pure (no
  mutation — assert event/run/schedule rows unchanged after
  the call); composes the shipped `storage/*` read API.
- `failure_monitor_scan`: detects an unacked overdue
  `admin_alert_sent`; an `admin_alert_acked` (correlates)
  clears it; below-threshold not flagged; pure (no emit / no
  event write).
- §11.1 byte-proof on slice 2: emit adapters + `sources/` +
  `cache.py` + `resolver.py` + `storage/*` + `ddl/`
  EMPTY-diff vs `v2-phase-14-complete`; phase-9–14
  fire-path / boundary / seam regression pins green.
- Alias-robust AST import-hygiene for the new package (no
  module-load `datetime.now` / `uuid4` / vendor-SDK).

## 6. CI guard checks

- `check_phase_scope.py --staged` and
  `--diff v2-phase-14-complete` exit 0; `PHASE_ALLOWLIST[15]`
  = exactly the full set (no agent mount).
- Alias-robust AST import-hygiene (11/12/13/14 carry-forward).
- `phase >= 9` doc-coupling (`ORI_SKIP_DOC_CHECK`
  harness-DENIED).
- `gen_docs` regen at closeout; stage INDEX/AGENTS_INVENTORY
  only if `files_changed != 0`.
- Full `tests/v2` green, no regression (2410 baseline +
  phase-15 adds).

## 7. Acceptance criteria for `v2-phase-15-complete`
(provisional — finalised after round-1 disposition)

1. Branch ahead of `v2-phase-14-complete` by N small
   per-slice commits.
2. Each primitive: pure read, correct over a seeded ledger,
   composes the shipped `storage/*` API (no SQL reimpl), no
   mutation.
3. `failure_monitor_scan`: pure detector; unacked-overdue
   detection + ack-clears + below-threshold-ignored; NO
   emit / event / binding wiring (build-the-layer, Q1/Q2).
4. Emit adapters + `sources/` + `cache.py` + `resolver.py` +
   `storage/*` + `ddl/` EMPTY-diff vs `v2-phase-14-complete`.
   No new EventKind, no v002.
5. Phase-11 `_fail_run` + phase-12 enforcement + phase-13
   state seam + phase-14 dedup / `_commit_success_atomic` /
   `paused_pending_policy` / `cancelled_reason`
   byte/behaviour-unchanged; no executor; retry chain still
   DEFERRED (no §12 step owns it).
6. No `datetime.now` / `uuid4` / vendor-SDK module-load in
   the new package; alias-robust AST pin green.
7. Phase guard `--staged` + `--diff v2-phase-14-complete`
   exit 0.
8. Full `tests/v2` green; no regression; walked with
   evidence.
9. plan ≡ shipped code ≡ design ≡ tag annotation, zero
   divergence (ONE reconciliation pass at closeout;
   semantic-intent, NOT literal-token — the lesson that
   recurred in phase 14).
10. Annotated tag `v2-phase-15-complete` (push gated on a
    reviewer CLOSEOUT PASS).

## 8. Tag annotation (draft — finalised at closeout)

```
v2 phase 15 complete

Observability (§12 step 15). FIVE pure read-only query /
aggregation primitives over the live EventLedger substrate
(written every fire by the phase-9–14 cores): schedule_status,
schedule_failures (per-schedule), schedule_history,
schedule_health (DI-clock), registry_status (reuses the
phase-6 registry_cache load_cache/is_stale — no re-impl).
Dedicated typed Pydantic result model per primitive
(ToolResponse was code-verified insufficient — NOT mutated).
Composes ONLY the shipped storage/events.py /
storage/schedules.py / registry_cache pure-read API, NO SQL
re-implementation, ZERO fire-path behaviour change (pure
side-channel). The background failure monitor ships as a
build-the-layer pure detector (DI-conn / DI-clock) — NO
periodic wiring, NO re-alert side-effect, reuses
admin_alert_sent / admin_alert_acked. No new EventKind, no
v002.

Deferred: schedule_diff — code-verified NO persisted
historical ScheduleSpec body to diff (schedules
one-row-per-id current-only; schedule_created
payload={hash,template}; no schedule_revised emitter;
execution_plans=ExecutionPlan-not-ScheduleSpec bodies); a
real §9 schedule_diff needs a spec-version store NOT shipped
(its own future design+DDL decision, no-v002-relitigation),
NOT a §15 pure-read primitive (slice-1 fork ruling α; a
metadata-only diff under the §9 name was explicitly REJECTED
as a degraded product / honest-scope debt — the phase-11
ChannelDigest-(b) anti-pattern). schedule_replay (touches the
dry-run/fire path + stored snapshots); the failure-monitor
periodic wiring + re-alert dispatch; external metrics/trace
exporter (not canonical §9); migration tooling (§12 step 16);
the reasoning/stateful-flow executor + the retry CHAIN (no
§12 step owns either).

Phase-9–14 fire path + emit adapters byte/behaviour-
unchanged; carried invariants intact.

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 15, §9
Plan:   docs/PHASE_15_PLAN.md
```

## 9. claude-reviewer round-1 disposition (CLOSED — Q1–Q7 RATIFIED)

Round 1 PASS (cleanest of the relay — first plan with ZERO
code-busted premise; verify-first genuinely honoured). Baked
VERBATIM (the phase-10–14 disposition-log discipline) so a
future drift is caught against the decision, not re-litigated.
Conditions binding.

- **Q1 = pure-read side-channel + BTL detector RATIFIED.**
  Cond: slice-2 §11.1 byte-proof + phase-9–14 boundary
  regression pins; primitives PURE (per-primitive test:
  event/run/schedule rows byte-unchanged post-call); detector
  NOT wired (periodic + re-alert deferred, closeout-recorded).
- **Q2 = pure detector only RATIFIED.** Cond: detector writes
  ZERO rows (pinned: no emit/event/DM/binding); reuses
  `admin_alert_sent` / `admin_alert_acked` (no new EventKind,
  no v002); re-alert + periodic wiring deferred — a future
  re-alert needing otherwise requires a verify-first fork.
- **Q3 = IN={status, failures, history, health,
  registry_status}; DEFER `schedule_replay` RATIFIED.**
  Replay touches the dry-run/fire path + stored snapshots
  (design §9), NOT pure-read. Cond: closeout §9
  reconciliation records the EXACT shipped set vs §9; the
  IN-set stays within §9 pure-read-ledger-aggregation intent
  (failures/history/health are decompositions of
  schedule_status-over-EventLedger — no NEW design surface
  beyond §9). **`schedule_diff` DEFERRED — see §9.1.**
- **Q4 = typed Pydantic results, no loose dicts RATIFIED
  (verify-first).** Slice-1 code-verified
  `authoring/responses.ToolResponse` insufficient (`ok`
  payload allowlist `{draft_id,schedule_id,spec,message}`,
  `spec` a loose `dict[str,Any]`, `extra="forbid"` +
  status-allowlist validator → no arbitrary typed payload) →
  a dedicated typed observability result model per primitive;
  `ToolResponse` NOT mutated (byte-safe; avoids the
  phase-7–14 authoring-contract regression surface).
- **Q5 = no agent mount RATIFIED** (`PHASE_ALLOWLIST[15]`
  mirrors `[13]`/`[14]` — no coordinator/run_bot/agent
  surface).
- **Q6 = external exporter OUT/deferred RATIFIED.** Not
  canonical §9. If ever ratified → `Protocol`/DI, NO
  vendor-SDK at module load (the carried alias-robust
  AST-pin discipline).
- **Q7 = `registry_status` REUSES the phase-6
  `registry_cache` read surface RATIFIED (verify-first).**
  Slice-1 code-verified `registry_cache/loader.py` exposes
  `load_cache` + `is_stale` (exported via `__init__`);
  `registry_status` REUSES them, NO re-implementation, NO DB.

### 9.1 Slice-1 fork ruling (CLOSED — α; `schedule_diff` premise code-verified-bust)

Slice-1 surfaced (pre-code, verify-first) a code-verified
bust of the ratified-Q3 premise that `schedule_diff` is a
pure-read-ledger-aggregation. Folded into the slice-1 commit
per the §0.3-class / phase-14 §9.2 discipline. Baked verbatim:

- **(a) Premise-bust (code-verified).** A §9 `schedule_diff`
  (unified diff of two historical ScheduleSpec bodies +
  tool-tag snapshots) needs persisted historical spec bodies.
  Verified NOWHERE queryable: `ddl/v001_initial.sql`
  `schedules` = `id TEXT PRIMARY KEY` (one row per id =
  CURRENT spec only; no `get_schedule_by_hash`);
  `schedule_created` event payload (`commit.py:140`) =
  `{"hash", "template"}` — NOT the spec body; NO
  `schedule_revised` event emitter exists in `authoring/`
  (the kind is reserved in the v001 CHECK but unwritten;
  `on_schedule_revised` is only a phase-5 binding re-register
  hook); `execution_plans` is hash-addressed but stores
  ExecutionPlan bodies, not ScheduleSpec bodies. The
  round-1 "RECOMMENDED IN: … `schedule_diff`" /
  "schedule_diff is a pure-read aggregation" premise is
  therefore FALSE — recorded verbatim in §0.2 Q3 with an
  inline **[SUPERSEDED]** so it reads as current NOWHERE
  outside that annotated record (the recurring phase-9–14
  stale-wording lesson — the very lesson that recurred in
  the phase-14 closeout).
- **(b) Ruling = (α).** `schedule_diff` DEFERRED exactly as
  `schedule_replay`. A real §9 `schedule_diff` requires a
  spec-version store that is NOT shipped — its own future
  design + DDL decision (no-v002-relitigation), NOT a §15
  pure-read primitive. Q3 IN-set amended **6→5**:
  `schedule_status`, `schedule_failures`, `schedule_history`,
  `schedule_health`, `registry_status`. The closeout §9
  reconciliation records `schedule_diff` with this EXACT
  code-evidence (same rationale-shape as `schedule_replay`).
- **(γ) REJECTED** — persisting spec bodies / a spec-version
  store = NEW design surface + storage/ddl touch + no-v002
  violation; collides with the ratified Q3 condition, the
  GO-slice-1 EMPTY-diff-vs-`v2-phase-14-complete` binding,
  and no-v002. Identical hard-invariant class to
  phase-14-(β).
- **(β) REJECTED** — a metadata-only `schedule_diff`
  (`parent_hash` chain + event metadata, NO bodies / tool-tag
  snapshots) ships materially LESS than the §9 stated
  semantics under the §9 name = honest-scope / expectation
  debt + later re-semantics / migration debt. Exactly the
  phase-11 ChannelDigest-(b) anti-pattern (a degraded product
  under a design name the substrate cannot honour). Recorded
  REJECTED explicitly so a future reader does NOT optimize
  toward a metadata-only diff.
- **Scope pin.** Slice-1 = the 5 pure-read primitives + typed
  models + tests ONLY. ZERO worker / emit / sources /
  storage / ddl / registry_cache touch (EMPTY-diff vs
  `v2-phase-14-complete`, verified). No new EventKind, no
  v002. `CONTRACTS_V2_DESIGN.md` §9 amendment (record the
  EXACT shipped 5-set + `schedule_diff`/`schedule_replay`
  deferred with substrate evidence) → the ONE closeout
  reconciliation pass, NOT slice 1.

## 10. Hard rules (carried forward from phases 9–14)

- Slice-gated; pause after each commit for claude-reviewer;
  no push without a CLOSEOUT PASS.
- `uv run python …` always; no `sed`; refresh
  `.docs_read_marker` before each commit; commit trailer
  `Co-Authored-By: Claude Opus 4.7 (1M context)
  <noreply@anthropic.com>`.
- Carried 5→15 invariants UNTOUCHED: resolver
  exactly-one-terminal / never-raise; cache ttl
  no-probe / probe-every-fire; §5.3.1 dirfd fence; phase-3
  CAS + phase-13 `state_read` / `state_write` /
  `_cross_fire_state_seam`; phase-11 `_fail_run` reasoning
  boundary (reason CODE byte-identical) + pure-snapshot-
  consumer; phase-12 read-only-reasoning enforcement
  (`_validate_reasoning_tool_mode` /
  `_validate_customflow_admin_friction`, distinct CODEs —
  only a boundary MESSAGE may ever change, never the CODE,
  not in phase 15); phase-14 emit dedup +
  `_commit_success_atomic` + `prior_emit_succeeded` +
  `paused_pending_policy` (FailurePolicy locus) +
  `cancelled_reason`; §11.1 additive — OneOff / v1 /
  phase-9–14 fire path + emit adapters
  (`slack_reminder.py` / `source_post.py`) +
  `sources/` / `cache.py` / `resolver.py` / `storage`
  byte/behaviour-unchanged; NO reasoning / stateful-flow
  executor anywhere (no §12 step owns it); the retry CHAIN
  is DEFERRED (no §12 step owns it); no new EventKind, no
  v002, no module-load `datetime.now` / `uuid4` /
  vendor-SDK.
- Fold plan / disposition reconciliation INTO the slice
  commit that introduces the mechanism (the phase-11 §0.3 /
  phase-12 §0.2 / phase-13 / phase-14 §9.1/§9.2 precedent),
  EXCEPT the ONE `docs/CONTRACTS_V2_DESIGN.md` pass at
  closeout.
- plan ≡ design ≡ code ≡ tag, SEMANTIC-INTENT (literal-token
  insufficient — the lesson that recurred in phase 14); the
  closeout REPO-WIDE sweep MUST catch any
  `lands in §12 step 15` / forward-ref that prior shipped
  phases now imply, and any new false claim.
- `.v2-current-phase`=15 + `PHASE_ALLOWLIST[15]` ride the
  plan commit; no agent mount (build-the-layer / authored
  surface only).
