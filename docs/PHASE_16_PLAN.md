# PHASE 16 PLAN — §12 step 16: Migration tooling (v1 → v2)

> Relay: slice-gated writer↔claude-reviewer via CONDUCTOR.
> All-remaining directive — **through phase 16 = §12 step 16
> = the LAST core step**. RELAY-TERMINUS (§8 below): step 17
> = Phase-2+ triggers = a large NEW scope, surfaced to Sergey;
> the step-17 decision does NOT bind until phase-16 closeout.
> Build phase 16 normally; do NOT continue past 16
> autonomously. Plan-review round 1 next.

§12 step 16 canonical (design line 1653): **Migration
tooling**: v1 contract → v2 wrap; legacy job → ScheduleSpec
import; ledger backfill. This is the HIGHEST-RISK phase — the
first legitimate v1 touch.

## 0. Scope refinement + open design questions (round-1 input)

### 0.0 Verified code-surface premises (pre-arm item 1 — done FIRST)

Every phase in this relay had ≥1 code-busted premise
(phase-14 spec-JSON; phase-15 schedule_diff). The phase-16
premises were grep-verified against the ACTUAL v1 + v2
surface BEFORE drafting:

- **v1 EXISTS + has a shipped pure-read API.**
  `app/contracts/` = `schema.py` (the `Contract` Pydantic
  model — `CronTrigger` / `OnDemandTrigger` / `EventTrigger`
  union, `InputSpec`, `OutputSpec`, `ReasoningStep`,
  `Retry`), `store.py` (`ContractStore` with
  `list_all() -> list[str]`, `load_latest(contract_id) ->
  Contract`, `load(contract_id, hash_) -> Contract`,
  `list_versions(contract_id)`, + `ContractError` /
  `ContractNotFound` / `ContractHashMismatch`), `executor.py`
  (APScheduler-driven `run_contract_fire` /
  `schedule_contract` — the LIVE v1 fire path),
  `loaders.py` / `store.py` / `emit.py` / `audit_mirror.py`
  / `admin_alert.py` / `_loader_context.py` / `__init__.py`.
  `app/tasks.py` (26 KB), `app/scheduler_instance.py`
  (830 B), `data/contracts/` (file store; empty on this dev
  box — migration is tooling, operates when contracts
  exist). **Migration READS v1 ONLY via the shipped
  `ContractStore` pure-read API + the `Contract` schema — NO
  v1 file/SQL/store re-implementation.**
- **v2 WRITE path is the shipped authoring/storage chain.**
  Draft → `to_spec` → freeze (`with_fresh_hash`) →
  `insert_schedule` + `append_event(SCHEDULE_CREATED)` (the
  phase-7/8/9 commit path) for the schedule; the shipped
  `app/v2/storage/events.py::append_event` for ledger
  backfill — NO SQL re-implementation (the phase-14 Q6 /
  phase-15 discipline).
- **`check_phase_scope` gating verified.**
  `is_forbidden(path, phase)` returns `False` for `phase >=
  9` (the v1 pre-cutover gate lifts at phase 9 — `app/v2/`
  §12.1 invariant 3); `evaluate` then still flags any
  changed file `not is_allowed(f, phase)`. So a *modified*
  v1 file at phase 16 would trip "not in
  `PHASE_ALLOWLIST[16]`" UNLESS the allowlist were widened —
  see Q5. The clean resolution: migration code is
  `app/v2/`-resident and reads v1 purely via runtime import,
  modifying ZERO v1 files, so NO v1 path ever enters
  `--staged` / `--diff` and the allowlist stays the standard
  `app/v2/`-scoped set (NOT widened).
- **`.v2-current-phase` = 15** → 16 this commit;
  `PHASE_ALLOWLIST` keyed by int, `[13]==[14]==[15]`
  full-set structure.

### 0.1 Classification — build-the-layer (pre-arm item 4, the recurring Q1)

A LIVE auto-running v1→v2 migration at boot is
high-blast-radius (it would mutate v2 state for every legacy
contract on every start, racing the live v1 fire path).
RECOMMENDED: **build-the-layer** — pure `Contract` →
`ScheduleSpec` import/wrap functions + a dry-run + an
explicit admin-GATED CLI / tool entry-point, NOT auto-invoked
at boot, NOT wired into `run_bot.py` / the binding. v1 keeps
running its existing contracts live, unchanged. Reviewer
ratifies the classification explicitly (do NOT assume).

### 0.2 v1 READ-ONLY invariant (pre-arm item 2 — BINDING)

Migration READS v1, WRITES v2. ZERO v1 mutation, ZERO v1
behaviour change. A byte-proof that `app/contracts/*` +
`app/tasks.py` + `app/scheduler_instance.py` +
`data/contracts/*` are UNMODIFIED vs `v2-phase-15-complete`
is BINDING every slice + closeout. v1 keeps firing its
contracts live throughout. The v2 side is additive via the
shipped authoring/storage path — NO new DDL / v002 unless a
verify-first fork explicitly ratifies it.

### 0.3 Open design questions for round 1 (verify-first, lowest-risk, bind)

- **Q1 — classification.** RECOMMENDED build-the-layer
  (§0.1): pure import/wrap + dry-run + GATED CLI, NOT
  auto-boot, NOT binding-wired. Reviewer ratifies live-vs-BTL.
- **Q2 — v1 READ-ONLY.** RECOMMENDED: read v1 ONLY via the
  shipped `ContractStore` pure-read API; modify ZERO v1
  files (byte-proof BINDING, §0.2). A future need to touch
  v1 = a verify-first fork (it would also break the v1
  READ-ONLY invariant — expected NEVER).
- **Q3 — mapping fidelity (honest-scope).** v1 `Contract` →
  v2 `ScheduleSpec`. `CronTrigger` maps cleanly to the v2
  cron trigger. `OnDemandTrigger` / `EventTrigger` have no
  clean v2 trigger analogue (v2 ships OneOff / Cron;
  event-bus wiring was never built — `executor.py` itself
  raises on `EventTrigger`). RECOMMENDED: migrate the
  cleanly-mappable subset (cron-triggered contracts) into a
  v2 draft via the shipped authoring path; the
  unmappable / heavy parts (`OnDemand` / `Event` triggers;
  the full `ReasoningStep` / `InputSpec` / `OutputSpec`
  ExecutionPlan port) are HONESTLY skipped + flagged in the
  migration report — NOT a lossy silent coercion under the
  "migrated" name (the phase-11-ChannelDigest-(b) /
  honest-scope discipline). Reviewer rules the exact cut;
  the round-1 disposition records it.
- **Q4 — ledger backfill provenance (§13 audit-truth).**
  Backfilled historical events use the shipped
  `append_event` (NO SQL reimpl) AND are honestly
  provenance-marked as backfill in the payload (e.g. a
  `"backfill": true` / source marker — NOT a new EventKind,
  NOT masquerading as live fires; mirrors the phase-14
  `cancelled_reason` audit-truth discipline). No new
  EventKind / v002 unless a verify-first fork ratifies it.
  RECOMMENDED: minimal honest provenance marker on
  backfilled rows; reviewer ratifies the marker shape.
- **Q5 — `PHASE_ALLOWLIST[16]` + v1-read transition point
  (pre-arm item 5; FLAGGED, surfaced).** The allowlist has
  been `app/v2/`-scoped every phase; a migration that READS
  v1 (`app/contracts/`) is OUTSIDE `app/v2/`. RECOMMENDED:
  because the migration code is `app/v2/`-resident and reads
  v1 purely via runtime import (Q2 — ZERO v1 files
  modified), NO v1 path ever appears in `--staged` /
  `--diff`, so `PHASE_ALLOWLIST[16]` stays the standard
  `app/v2/`-scoped full set (mirrors `[13]`/`[14]`/`[15]`) —
  NOT a silent widening. The v1-read is an import-time
  dependency, not a tracked file change; the §0.2 v1
  byte-proof is what makes this sound. Reviewer ratifies
  this treatment (an alternative — an explicit read-only v1
  allowlist entry — is recorded as the rejected option with
  rationale, NOT silently chosen).
- **Q6 — CLI gating + idempotency.** RECOMMENDED: the
  migration entry-point is an explicit admin-gated tool with
  a dry-run-first flow; idempotent (re-running does not
  double-create v2 schedules nor double-backfill — keyed off
  the v2 hash-addressed identity + a ledger backfill marker
  check; conceptually the phase-14 idempotency discipline,
  NO new mechanism). Reviewer ratifies.
- **Q7 — dry-run.** RECOMMENDED: a dry-run mode previews the
  v2 `ScheduleSpec`(s) + the backfill plan WITHOUT any write
  (reuse the shipped dry-run / draft surface where it
  composes; no fire-path touch). Reviewer ratifies.

## 1. Scope statement

### In scope (phase 16), assuming Q1 BTL / Q2–Q7 RECOMMENDED

1. **Pure v1→v2 mapping/wrap** — `Contract` (read via the
   shipped `ContractStore`) → v2 `ScheduleSpec` draft via
   the shipped authoring path, for the cleanly-mappable
   subset (Q3); unmappable parts honestly flagged.
2. **Dry-run + GATED migration entry-point** — preview
   (no write) + an explicit admin-gated commit; idempotent;
   NOT auto-boot, NOT binding-wired (build-the-layer, Q1/Q6).
3. **Ledger backfill** — historical v1 fires → v2 EventLedger
   via the shipped `append_event`, honestly
   backfill-provenance-marked (Q4); NO SQL reimpl, no new
   EventKind/v002.
4. **Hygiene + docs.** Alias-robust AST import-hygiene pin
   for any new module. `PHASE_ALLOWLIST[16]` +
   `.v2-current-phase`→16 land in the plan commit (this
   commit). `gen_docs` regen + stage at closeout.

### Out of scope (phase 16) — deferred with rationale

- **v1 mutation of ANY kind** — v1 READ-ONLY invariant
  (§0.2); v1 keeps running unchanged.
- **`OnDemand` / `Event` trigger migration + the full
  `ReasoningStep`/`InputSpec`/`OutputSpec` ExecutionPlan
  port** (Q3) — no clean v2 analogue / heavy; honestly
  flagged, not silently coerced; their own future work.
- **Auto-migration at boot / binding wiring** (Q1) —
  high-blast; build-the-layer GATED CLI only.
- **Step 17 (Phase-2+ interval/conditional/branch/loop/
  parallel triggers)** — a large NEW scope; RELAY-TERMINUS
  (§8); Sergey decides, does not bind until phase-16
  closeout.
- Reasoning/stateful-flow executor + the retry CHAIN — no
  §12 step owns either (carried).

## 2. New file paths (provisional — finalised per slice)

- `app/v2/migration/` (new package) — pure `Contract`→
  `ScheduleSpec` mapping + dry-run + the GATED entry-point +
  the backfill helper. Pure / DI-conn, no vendor SDK, no
  module-load nondeterminism. Reads v1 via
  `from app.contracts.store import ContractStore` (runtime
  import; ZERO v1 file change).
- `tests/v2/test_migration_*.py` — mapping fidelity +
  honest-skip + dry-run-no-write + backfill provenance +
  v1-byte-proof + a folded alias-robust AST import-hygiene
  pin.

## 3. Module APIs (sketch — finalised per slice)

- `contract_to_spec_draft(contract: Contract, *, …) ->
  <DraftOrTypedResult>` — pure map of the mappable subset;
  unmappable → a typed "skipped + reason" result (honest,
  no silent coercion).
- `migration_dry_run(*, store, conn, …) ->
  <MigrationPlanResult>` — preview specs + backfill plan,
  ZERO write.
- `migrate_v1_to_v2(*, store, conn, …, confirm) ->
  <MigrationResult>` — GATED (explicit confirm), idempotent,
  composes the shipped authoring/storage write path +
  `append_event` backfill (provenance-marked). NOT
  auto-invoked.

## 4. Slice ordering + commit cadence (draft — reviewer finalises)

0. **plan + phase transition** (this commit; NOT pushed) —
   `docs/PHASE_16_PLAN.md`, `PHASE_ALLOWLIST[16]`,
   `.v2-current-phase`→16. Design doc NOT touched here (the
   ONE reconciliation pass is closeout).
1. **Pure mapping + dry-run** — `contract_to_spec_draft` +
   `migration_dry_run` (decision/pure layer, ZERO write,
   ZERO v1 mutation). Mapping-fidelity + honest-skip +
   dry-run-no-write tests + the folded hygiene pin.
   **LANDED** (slice-1; folded in-commit per the phase-11
   §0.3 / phase-14 §9.2 / phase-15 §9.1 discipline). New
   `app/v2/migration/` package (`__init__.py`,
   `results.py`, `mapper.py`). NO code-busted premise this
   slice (the v1 read shape matched §0.0). Reads v1 ONLY
   via `from app.contracts.store import ContractStore`
   (`list_all` / `load_latest`) + `app.contracts.schema`
   models — NEVER `app.contracts.executor` / `app.tasks` /
   `app.scheduler_instance` / any v1 write path (pinned by
   an alias-robust AST import scan). `assess_contract` is
   the pure structural Q3 cut collecting ALL honest skip
   reasons (the `validate_schedule_spec` all-issues
   discipline): SKIPPED for a non-cron trigger /
   reasoning-bearing / deterministic-inputs / multi-emit /
   gated-or-abort emit / sub-8 description / non-default
   `on_failure` / non-default `acceptance` — NEVER silently
   coerced or lossily partial-migrated.
   `contract_to_schedule_spec` maps ONLY a
   `MIGRATABLE_WITH_BINDING` contract → an in-memory
   UNFROZEN v2 `ScheduleSpec` (`hash==""` — slice-1 is
   PURE, no `with_fresh_hash`, ZERO write); a SKIPPED
   contract raises the typed `ContractNotMigratable`. The
   two v2 fields v1 does not carry 1:1
   (`owner.platform` — v1 `author` has no platform;
   `delivery.target_session_id` — the v1 emit destination
   is an adapter-specific template arg, NOT a schema field)
   are explicit operator-supplied `MigrationBinding`
   values, NEVER fabricated. `migration_dry_run` enumerates
   `store.list_all()`→`load_latest()`→`assess` into a typed
   `MigrationPlanReport` (Q7 honest MIGRATABLE-vs-SKIPPED;
   ZERO write — the functions take no DB conn). Typed
   Pydantic result models throughout (Q4/§3.5 — no loose
   dicts). `tests/v2/test_migration_mapping.py`: assess
   migratable + per-reason honest SKIP + all-reasons-not-
   first-fail + faithful-unfrozen map + refuse-skipped +
   dry-run enumeration over a read-only stub store + the
   v1-READ-ONLY-import pin + the folded alias-robust AST
   module-load-hygiene pin. 2438 v2 tests, 0 fail, 0
   regression (2425 phase-15 + 13 slice-1). **v1 byte-proof
   (BINDING): `app/contracts/*` + `app/tasks.py` +
   `app/scheduler_instance.py` + `data/contracts/*` ALL
   0-diff vs `v2-phase-15-complete`.** Carried 5→16 +
   phase-15 `observability/` + `enums.py` 0-diff; no new
   EventKind, no v002. NO gated-write / NO backfill / NO
   CLI (slice 2). `PHASE_ALLOWLIST[16]` unchanged
   (`app/v2/` covers `migration/`; the v1-read is an
   import dependency, ZERO v1 files changed — Q5 holds).
2. **GATED migrate + ledger backfill** — `migrate_v1_to_v2`
   (explicit confirm, idempotent) composing the shipped
   authoring/storage + `append_event` (backfill
   provenance-marked). Carries the v1 byte-proof + the
   phase-9–15 boundary regression pins (the v1-read slice).
3. **closeout** — full `tests/v2`, §7 acceptance walk, phase
   guards `--staged` + `--diff v2-phase-15-complete`,
   `gen_docs` regen+stage, REPO-WIDE SEMANTIC-INTENT
   stale-wording sweep (MUST catch any `lands in §12 step
   16` / forward-ref prior shipped phases now imply + any
   new false claim), the ONE `docs/CONTRACTS_V2_DESIGN.md`
   reconciliation pass (record the EXACT shipped migration
   surface + the honestly-skipped v1 subset + backfill
   provenance + build-the-layer GATED-not-auto), annotated
   tag `v2-phase-16-complete` (gated on a claude-reviewer
   CLOSEOUT PASS). **RELAY-TERMINUS: phase-16 closeout = v2
   core plan complete; do NOT continue to step 17
   autonomously — Sergey decides.**

## 5. Test inventory (highlights)

- Mapping: a cron `Contract` → a valid v2 `ScheduleSpec`
  draft (identity + trigger + delivery preserved);
  `OnDemand`/`Event` → honest "skipped + reason", NOT a
  coerced spec.
- Dry-run: ZERO write — v2 `schedules` / `events` row counts
  + the full-table fingerprint byte-unchanged after a
  dry-run.
- GATED migrate: requires explicit confirm; idempotent
  (second run creates no duplicate v2 schedule, no duplicate
  backfill); composes the shipped authoring/storage path.
- Backfill provenance (§13 audit-truth): backfilled events
  carry the honest backfill marker, written via the shipped
  `append_event`; NOT indistinguishable from live fires; no
  new EventKind.
- **v1 byte-proof (BINDING)**: `app/contracts/*` +
  `app/tasks.py` + `app/scheduler_instance.py` +
  `data/contracts/*` EMPTY-diff vs `v2-phase-15-complete`.
- §11.1 / phase-9–15 boundary regression pins green.
- Alias-robust AST import-hygiene for the new package.

## 6. CI guard checks

- `check_phase_scope.py --staged` and
  `--diff v2-phase-15-complete` exit 0; `PHASE_ALLOWLIST[16]`
  = the standard `app/v2/`-scoped full set (Q5 — NOT widened
  for v1; migration reads v1 via import, ZERO v1 files
  changed).
- Alias-robust AST import-hygiene (11–15 carry-forward).
- `phase >= 9` doc-coupling (`ORI_SKIP_DOC_CHECK`
  harness-DENIED).
- `gen_docs` regen at closeout; stage INDEX/AGENTS_INVENTORY
  only if `files_changed != 0`.
- Full `tests/v2` green, no regression (2425 baseline +
  phase-16 adds).

## 7. Acceptance criteria for `v2-phase-16-complete`
(provisional — finalised after round-1 disposition)

1. Branch ahead of `v2-phase-15-complete` by N small
   per-slice commits.
2. Pure mapping correct for the mappable subset;
   unmappable v1 honestly flagged (no silent lossy
   coercion).
3. Dry-run = ZERO write (fingerprint byte-unchanged);
   GATED migrate requires explicit confirm + is idempotent.
4. Ledger backfill via the shipped `append_event`, honestly
   provenance-marked (§13); no new EventKind, no v002.
5. **v1 byte-proof (BINDING)**: `app/contracts/*` /
   `app/tasks.py` / `app/scheduler_instance.py` /
   `data/contracts/*` EMPTY-diff vs `v2-phase-15-complete`
   — ZERO v1 mutation / behaviour change.
6. Carried 5→16: emit adapters + `sources/` + `cache.py` +
   `resolver.py` + `storage/*` + `ddl/` + `worker.py` +
   `registry_cache/` + the phase-15 `observability/`
   primitives + detector EMPTY-diff / behaviour-unchanged;
   phase-11/12/13/14 boundaries byte-unchanged; no executor;
   retry chain DEFERRED.
7. No `datetime.now`/`uuid4`/vendor-SDK module-load in the
   new package; alias-robust AST pin green.
8. Phase guard `--staged` + `--diff v2-phase-15-complete`
   exit 0; `PHASE_ALLOWLIST[16]` NOT widened for v1 (Q5).
9. Full `tests/v2` green; no regression; walked with
   evidence.
10. plan ≡ shipped code ≡ design ≡ tag annotation, zero
    divergence (ONE reconciliation pass at closeout;
    semantic-intent, NOT literal-token).
11. Annotated tag `v2-phase-16-complete` (push gated on a
    reviewer CLOSEOUT PASS). RELAY-TERMINUS recorded.

## 8. Tag annotation (draft — finalised at closeout)

```
v2 phase 16 complete — migration tooling (§12 step 16); v2
core plan COMPLETE

Build-the-layer v1→v2 migration: pure Contract→ScheduleSpec
mapping (read v1 via the shipped ContractStore pure-read API
ONLY — v1 byte-untouched) + a dry-run + an explicit
admin-GATED, idempotent migrate entry-point composing the
shipped authoring/storage write path; ledger backfill via the
shipped append_event, honestly backfill-provenance-marked
(§13 audit-truth — NOT masquerading as live fires). NOT
auto-invoked at boot, NOT binding-wired. The cleanly-mappable
subset (cron contracts) migrates; OnDemand/Event triggers +
the full ReasoningStep/InputSpec/OutputSpec port are HONESTLY
skipped+flagged (no silent lossy coercion). v1 READ-ONLY:
app/contracts/* + app/tasks.py + app/scheduler_instance.py +
data/contracts/* byte-unchanged. No new EventKind, no v002.

Phase-9–15 fire path + emit adapters + observability
primitives/detector byte/behaviour-unchanged; carried
invariants intact; no executor; retry chain DEFERRED.

§12 step 16 is the LAST core step — the v2 core plan is
COMPLETE. Step 17 (Phase-2+ triggers) is a separate NEW
scope, Sergey's decision; not entered autonomously.

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 16
Plan:   docs/PHASE_16_PLAN.md
```

## 9. claude-reviewer round-1 disposition (CLOSED — Q1–Q7 RATIFIED)

Round 1 PASS (second consecutive clean plan-round — the
§0.0 v1-premise independently confirmed genuine;
verify-first genuinely honoured; no code-busted premise).
Baked VERBATIM (the phase-10–15 disposition-log discipline)
so a future drift is caught against the decision, not
re-litigated. Conditions binding. No SUPERSEDED record
needed this phase (no bust); the discipline stands for any
future one.

- **Q1 = build-the-layer GATED CLI RATIFIED.** Cond: NO
  auto-boot / binding wiring; the migrate path is
  admin-GATED + idempotent + dry-run-first (any wiring
  deferred, closeout-recorded).
- **Q2 = v1 READ-ONLY via the shipped `ContractStore`
  import RATIFIED (strongest cond).** Migration imports
  ONLY the `ContractStore` READ methods — NEVER
  `executor.py` / `tasks.py` / any v1 write path; ZERO v1
  mutation / behaviour-change; BINDING byte-proof every
  slice + closeout (`app/contracts/*` + `app/tasks.py` +
  `app/scheduler_instance.py` + `data/contracts/*` 0-diff
  vs `v2-phase-15-complete`).
- **Q3 = honest mapping cut RATIFIED.** Cron +
  v2-expressible shape migrates; reasoning-bearing /
  OnDemand / Event / un-portable `InputSpec` / `OutputSpec`
  SKIPPED + explicitly FLAGGED (per-contract skip-reason in
  the dry-run report) — NEVER silently coerced / dropped
  (ChannelDigest-(b) + §13 audit-truth: a migrated v2 spec
  must not misrepresent the v1 contract). Cond: a
  partial / lossy migration of an un-portable contract is
  REJECTED (skip+flag); the closeout §12-step-16
  reconciliation records the exact migratable subset vs
  honestly-deferred constructs (no v1-fully-migrates
  over-claim).
- **Q4 = backfill provenance RATIFIED.** Via the shipped
  `append_event` (no SQL reimpl); backfilled events
  honestly provenance-marked in PAYLOAD
  (`backfill` / `migrated_from`), NOT a new EventKind, NOT
  masquerading as live fires (phase-14 `cancelled_reason`
  §13 discipline). Cond: a consumer can distinguish
  backfilled vs live-fired; no new EventKind / v002; the
  backfill window is an explicit GATED-CLI parameter with a
  documented bounded default (NOT implicit all-history).
- **Q5 = allowlist treatment RATIFIED (verified sound).**
  Cond: the v1-READ-ONLY byte-proof is BINDING — if any v1
  file ever appears in a migration diff, Q5 collapses and
  the allowlist gate correctly HOLDs; the Q5 rationale +
  the rejected alternative (an explicit read-only v1
  allowlist entry) are recorded verbatim (§0.3 Q5 / §6 /
  §10 — NOT a silent widening).
- **Q6 = gated + idempotent CLI RATIFIED.** Cond:
  idempotency PINNED (re-run = NO duplicate v2 schedule, NO
  duplicate backfilled event — reuse the phase-14
  idempotency / content-addressed discipline; a test);
  admin-gated (explicit invocation, not boot-wired); the
  write requires an explicit flag (dry-run default).
- **Q7 = dry-run RATIFIED.** Cond: dry-run is PURE (ZERO
  v2 write, ZERO v1 mutation — byte-proof + purity pin);
  the report = the honest Q3 migratable-vs-skipped
  enumeration; the write path is strictly slice-2, gated,
  idempotent.

**RELAY-TERMINUS re-affirmed (binds at phase-16 closeout,
not before).** §12 step 16 is the LAST core step;
phase-16 closeout = the v2 §12 core plan COMPLETE. Step 17
(Phase-2+ triggers) = a large NEW scope, NOT within the
all-remaining directive's natural terminus, NOT entered
autonomously. claude-reviewer issues an explicit flag to
conductor / Sergey at phase-16 closeout: core plan
complete; step-17 entry requires an explicit Sergey
decision. Build phase 16 normally.

## 10. Hard rules (carried forward from phases 9–15)

- Slice-gated; pause after each commit for claude-reviewer;
  no push without a CLOSEOUT PASS.
- `uv run python …` always; no `sed`; refresh
  `.docs_read_marker` before each commit; commit trailer
  `Co-Authored-By: Claude Opus 4.7 (1M context)
  <noreply@anthropic.com>`.
- **v1 READ-ONLY invariant (BINDING, §0.2)** —
  `app/contracts/*` / `app/tasks.py` /
  `app/scheduler_instance.py` / `data/contracts/*`
  byte-unmodified every slice + closeout; v1 keeps running
  live unchanged.
- Carried 5→16 invariants UNTOUCHED: resolver
  exactly-one-terminal / never-raise; cache ttl; §5.3.1
  dirfd; phase-3 CAS + phase-13 `state_read` /
  `state_write` / `_cross_fire_state_seam`; phase-11
  `_fail_run` reasoning boundary (reason CODE byte-
  identical); phase-12 read-only-reasoning enforcement
  (distinct CODEs — only a boundary MESSAGE may change,
  never the CODE, not in phase 16); phase-14 emit dedup +
  `_commit_success_atomic` + `prior_emit_succeeded` +
  `paused_pending_policy` + `cancelled_reason`; phase-15
  5 observability primitives + `failure_monitor_scan`
  detector; §11.1 additive — OneOff / v1 / phase-9–15 fire
  path + emit adapters byte/behaviour-unchanged; NO
  reasoning / stateful-flow executor anywhere (no §12 step
  owns it); the retry CHAIN is DEFERRED (no §12 step owns
  it); no new EventKind, no v002, no module-load
  `datetime.now` / `uuid4` / vendor-SDK — unless a
  verify-first fork explicitly ratifies (the
  phase-11-Option-B / phase-14-(α) / phase-15-(α)
  discipline).
- Fold plan / disposition reconciliation INTO the slice
  commit that introduces the mechanism (the phase-11 §0.3 /
  phase-14 §9.2 / phase-15 §9.1 precedent), EXCEPT the ONE
  `docs/CONTRACTS_V2_DESIGN.md` pass at closeout.
- plan ≡ design ≡ code ≡ tag, SEMANTIC-INTENT (literal-token
  insufficient); the closeout REPO-WIDE sweep MUST catch any
  `lands in §12 step 16` / forward-ref prior shipped phases
  now imply, and any new false claim.
- `.v2-current-phase`=16 + `PHASE_ALLOWLIST[16]` ride the
  plan commit; NO agent mount; NOT widened for v1 (Q5 —
  migration reads v1 via import, ZERO v1 files changed).
- **RELAY-TERMINUS**: §12 step 16 is the LAST core step.
  phase-16 closeout = v2 core plan COMPLETE. Step 17 =
  Phase-2+ triggers = a large NEW scope (Sergey's decision,
  surfaced now); it does NOT bind until phase-16 closeout;
  do NOT continue past phase 16 autonomously.
