# Phase 13 plan — §12 step 13: cross-fire state (state_read / state_write + CAS)

Status: **DRAFT for claude-reviewer plan-review round 1.** Not
pushed. Same loop contract as phases 5–12: plan-review rounds
→ per-slice implement + review → closeout (full v2 suite +
acceptance walk + phase guard + gen_docs + repo-wide
semantic-intent sweep + the ONE `docs/CONTRACTS_V2_DESIGN.md`
reconciliation pass + annotated tag `v2-phase-13-complete`,
tag push gated on a claude-reviewer CLOSEOUT PASS).

`docs/CONTRACTS_V2_DESIGN.md` §12 step 13 is the canonical
scope source:

> 13. **Cross-fire state with locks + CAS**: state_read /
> state_write primitives backed by `schedule_state` table +
> CAS.

Dependency (design §12): *step 13 depends on 3* (the storage
layer). That layer shipped in phase 3 (slice 7).

Design references: §6.5 (cross-fire state with CAS), D5
(`schedule_state` per-schedule), §12 step 13.

---

## 0. Scope refinement + open design questions (round-1 input)

### 0.0 What already exists (do NOT rebuild)

- `app/v2/ddl/v001_initial.sql` — `schedule_state` table
  (per-schedule `(schedule_id, key)` key/value + `version`,
  `written_at`, `written_by_run`).
- `app/v2/models/state.py` — `ScheduleState` Pydantic model.
- `app/v2/storage/schedule_state.py` (phase-3 slice-7) — the
  STORAGE CAS primitive:
  - `get_state(conn, *, schedule_id, key) -> ScheduleState |
    None` (read-only, JSON-decoded value).
  - `set_state_cas(conn, …, expected_version=…) -> bool`
    (atomic compare-version-and-set; True iff exactly one row
    affected; False on stale-version; **NO internal retry —
    caller owns the retry policy**) + `StateRunMismatchError`
    (lineage cross-check: `written_by_run` must belong to
    `schedule_id`).

**Missing (= phase-13 deliverable): the §6.5 RUNTIME
primitives** — `state_read` / `state_write` — and their
fire-path seam. The storage CAS exists; the §6.5
runtime-facing contract + the "locks" semantics + the
deterministic-loader integration point are not yet wired.

### 0.1 Open design questions for round 1

Each carries a RECOMMENDED disposition (reviewer ratifies or
overrides — the phase-11/12 §0 pattern). Nothing below is
unilaterally decided.

- **Q1 — no stateful-flow EXECUTOR consumes the primitives
  (the central scope question, mirrors phase-12 Q1).** §6.5:
  *"LLM never touches state. Computed by deterministic loader
  logic."* Use-case 12 (Series with cross-fire state) is an
  ExecutionPlan doing `state_read → pick → state_write`. But
  there is no reasoning/flow executor (phase-11 emit-only;
  phase-12 shipped enforcement, NO executor — no §12 step
  owns it) and the stateful `RecurringSeriesFromSource`
  progress was explicitly deferred to step-13 *needing*
  cross-fire state. Options:
  - **(a) build-the-layer + seam — RECOMMENDED.** Ship the
    §6.5 `state_read` / `state_write` RUNTIME primitives (a
    thin, typed layer over the shipped
    `get_state`/`set_state_cas`) + a worker/flow SEAM where a
    future deterministic state-loader would consult them,
    pure + fully tested, NOT fired end-to-end (no executor /
    no stateful-flow consumer). Mirrors the phase-10/12
    build-the-layer discipline. Stateful
    `RecurringSeriesFromSource` progress stays deferred until
    an executor lands.
  - (b) primitives-only, no seam.
  - (c) primitives + a narrow stateless integration.
  - *Rationale for (a):* maximises shippable, testable value
    without the executor; reuses the reviewer-approved
    build-the-layer pattern; zero regression to the
    phase-11/12 fire-path/boundary.
- **Q2 — "locks" semantics.** §12 step-13 title says "with
  locks + CAS" but §6.5 specifies only CAS. RECOMMENDED:
  CAS (`version`) IS the concurrency-control mechanism;
  "locks" = the EXISTING per-schedule single-flight run
  claim (already shipped — `claim_run`, no other run on the
  schedule claimed/running). NO new advisory
  per-`(schedule_id,key)` lock table/primitive (CAS makes it
  unnecessary; an extra lock would duplicate the claim
  invariant). The closeout design reconciliation records
  this reading so §12 step-13 never reads as "a new lock
  primitive shipped".
- **Q3 — `state_write` error/outcome surface.** RECOMMENDED:
  `state_write(...expected_version=None)` returns a typed
  outcome (`written` with new version vs `stale_version`);
  delegates to `set_state_cas` (NO internal retry — caller
  owns it, the shipped contract); `StateRunMismatchError`
  propagates (lineage audit-truth). `state_read` returns the
  §6.5 shape `{value, version, written_at, written_by_run}`
  or `None` (thin map over `get_state`/`ScheduleState`).
- **Q4 — D5 per-schedule scope; shared namespace OUT.**
  RECOMMENDED: keep `schedule_state` strictly per-schedule
  (D5). The cross-schedule shared-namespace idea (design
  open-question #6) is explicitly OUT of scope / deferred
  (design open question, not a step-13 deliverable).
- **Q5 — module placement.** Where do the runtime primitives
  sit — a new `app/v2/runtime/state.py`, or an additive
  extension of `app/v2/storage/schedule_state.py`? Reviewer's
  call in round 1 (mirrors the phase-12 §2 reviewer-choice).
  RECOMMENDED: a new `app/v2/runtime/state.py` so the
  storage CAS stays a pure storage primitive and the
  runtime-facing `state_read`/`state_write` (DI conn, typed
  outcomes) layer on top.
- **Q6 — §11.1 backward-compat.** RECOMMENDED: the
  primitives are purely additive; NO existing fire path
  consumes cross-fire state (stateful flow deferred), so
  OneOff / v1 / phase-9–12 fire path + emit/cache/resolver
  are byte/behaviour-unchanged. Pinned by a regression test
  + the 5→13 byte-proof at closeout.

---

## 1. Scope statement

### In scope (phase 13), assuming Q1(a) / Q2–Q6 RECOMMENDED
(reviewer may revise):

1. **§6.5 runtime primitives.** `state_read(schedule_id,
   key)` → `{value, version, written_at, written_by_run}` or
   `None`; `state_write(schedule_id, key, value,
   expected_version=None)` → typed outcome (written /
   stale_version), CAS when `expected_version` given. Thin,
   typed, DI-conn layer over the shipped
   `get_state`/`set_state_cas`; NO policy/SQL re-implemented;
   NO internal retry (caller owns it). `StateRunMismatchError`
   propagates.
2. **Build-the-layer seam.** The worker/flow seam where a
   future deterministic state-loader would
   `state_read → pick → state_write`, wired but DEAD (no
   executor — Q1a); a seam test pins it reachable-but-deferred
   and that the live fire path never consults it.
3. **Hygiene + docs.** Alias-robust AST import-hygiene pin
   for any new module (no `datetime.now`/`uuid4`/vendor-SDK
   module-load — the phase-11/12 detector). `PHASE_ALLOWLIST[13]`
   + `.v2-current-phase`→13 land in the plan commit (this
   commit). `gen_docs` regen + stage at closeout.

### Out of scope (phase 13) — deferred with rationale

- **The deterministic stateful-flow / reasoning EXECUTOR**
  and stateful `RecurringSeriesFromSource` progress — no §12
  step owns the executor (carried from phase-11/12).
- **Cross-schedule shared state namespace** (design
  open-question #6) — per-schedule D5 only.
- **New advisory lock primitive** — CAS + the existing
  single-flight claim suffice (Q2).
- Idempotency/cancellation (step 14), observability (15),
  migration tooling (16).

---

## 2. New file paths (provisional — finalised per slice)

- `app/v2/runtime/state.py` (Q5 — or an additive extension
  of `app/v2/storage/schedule_state.py`; reviewer's call) —
  the §6.5 `state_read`/`state_write` runtime primitives +
  typed outcomes.
- `tests/v2/test_runtime_state.py` — primitive + CAS +
  lineage + caller-owns-retry unit tests.
- `tests/v2/test_phase13_import_hygiene.py` — alias-robust
  AST pin (carry the phase-11/12 detector).
- Seam: `app/v2/runtime/worker.py` touched ONLY for the
  additive dead seam (NO live-path behaviour change).

## 3. Module APIs (sketch — finalised per slice)

- `state_read(conn, *, schedule_id, key) -> StateView | None`
  — `StateView{value, version, written_at, written_by_run}`.
- `state_write(conn, *, schedule_id, key, value,
  written_by_run, expected_version=None) -> StateWriteOutcome`
  — `{status: "written"|"stale_version", version}`.
- Worker seam (build-the-layer): a guard/consult point
  positioned where a future deterministic state-loader would
  run, behind the unchanged phase-11/12 boundaries.

## 4. Slice ordering + commit cadence (draft — reviewer
finalises)

0. **plan + phase transition** (this commit; NOT pushed) —
   `docs/PHASE_13_PLAN.md`, `PHASE_ALLOWLIST[13]`,
   `.v2-current-phase`→13. Design doc NOT touched here (the
   ONE reconciliation pass is at closeout).
1. **§6.5 runtime primitives** (`state_read`/`state_write` +
   typed outcomes) over the shipped storage CAS — unit tests
   (CAS hit/stale, lineage `StateRunMismatchError`,
   caller-owns-retry, None) + alias-robust AST hygiene pin.
   **LANDED** — `app/v2/runtime/state.py` (Q5: new module).
   `StateView{value,version,written_at,written_by_run}` +
   `StateWriteOutcome{status: written|stale_version,
   version}` (frozen). `state_read` thin-maps `get_state`;
   `state_write` delegates to `set_state_cas` — NO SQL, NO
   DDL, `storage/schedule_state.py` + `models/state.py` +
   `ddl/` BYTE-UNTOUCHED (empty diff vs `v2-phase-12-complete`).
   `expected_version=None`/`0` ⇒ first-write sentinel
   (`set_state_cas(expected_version=0)`; a present row ⇒
   `stale_version`, NO read-then-write so no TOCTOU, NO
   unconditional overwrite); `>=1` ⇒ CAS. NO internal
   retry/spin — a refused write returns `stale_version`
   (`version=None`) from a SINGLE call (racing-writers test
   pins the loser does not spin). `StateRunMismatchError` /
   `NaiveDatetimeError` / `ValueError`(neg version) /
   `IntegrityError` / `ConnectionNotReady` PROPAGATE
   (no swallow). DI `now` (no `datetime.now` module-load).
   `tests/v2/test_runtime_state.py` + alias-robust
   `test_phase13_import_hygiene.py`; the shipped phase-3
   `test_storage_schedule_state.py` still green (CAS contract
   intact). ZERO sources/resolver/cache/emit/worker touch;
   `PHASE_ALLOWLIST[13]` unchanged (no new surface path).
2. **Build-the-layer worker/flow seam** (Q1a) — wired, DEAD;
   phase-11 `_fail_run` + phase-12 enforcement boundaries
   regression-pinned UNCHANGED; seam test
   reachable-but-deferred + live-path-not-consulted AST pin.
   **LANDED** — `app/v2/runtime/worker.py` private
   `_cross_fire_state_seam(conn, *, schedule_id, key) ->
   StateView | None`: STRICT read-only delegation to the
   slice-1 `state_read` (NO CAS/policy re-declared; the
   `pick`+`state_write` half is the deferred loader's job and
   is deliberately NOT done). A seam comment marks where a
   future deterministic state-loader (§6.5 / use-case 12)
   would `state_read → pick → state_write`. **DEAD on the
   live path** — AST scan pins `_dispatch_emit_branch` never
   CALLs it (comment-mention expected). worker.py touched
   ONLY: the slice-1 import + the seam comment + the additive
   dead method — NO live-path control-flow change. phase-11
   reason CODE `reasoning_unsupported_pending_step_12`
   BYTE-IDENTICAL; phase-12 enforcement code (the
   `_validate_reasoning_tool_mode` /
   `_validate_customflow_admin_friction` rules + their
   distinct codes) UNTOUCHED. NO stateful-flow executor
   shipped (Q1a). §11.1: OneOff/v1/phase-9–12 fire path +
   `storage/schedule_state.py`/`models/state.py`/`ddl/`/
   `sources/`/`emit/` EMPTY diff vs `v2-phase-12-complete`.
   `tests/v2/test_worker_state_seam.py`; phase-11/12
   regression pins UNMODIFIED + green. No new module ⇒
   `PHASE_ALLOWLIST[13]` unchanged.
3. **closeout** — full `tests/v2`, §7 acceptance walk, phase
   guards, `gen_docs` regen+stage, REPO-WIDE semantic-intent
   stale-wording/inconsistency sweep (the phase-9/10/11/12
   lesson) **including the carried phase-12 non-binding 🔵**
   (`app/v2/tool_tags.py` `_ADMIN_APPROVAL_TAGS` comment
   "trigger the admin-approval friction gate" → tighten to
   "§5.9 advisory friction (signal; the gate is conceptual /
   deferred)"), the ONE `docs/CONTRACTS_V2_DESIGN.md`
   reconciliation pass (§6.5 LIVE note + §12 step-13
   shipped-scope: primitives + build-the-layer seam, NO
   executor; "locks" = CAS + existing claim, no new lock;
   per-schedule D5; shared namespace deferred), annotated
   tag `v2-phase-13-complete` (gated on CLOSEOUT PASS).

   **LANDED (closeout).** Full `tests/v2` **2383 passed**, 0
   fail, 0 regression. Phase guards `--staged` +
   `--diff v2-phase-12-complete` exit 0. `gen_docs`
   `files_changed=0` (phase-13 modules are not gen_docs
   symbols — nothing to stage). REPO-WIDE semantic-intent
   sweep: NO phase-13 falsified forward-ref ("stateful series
   live" / "executor lands" / "new lock primitive") survives
   in `app/v2/` or `tests/v2/` — slice-1/2 wrote accurate
   DEAD/deferred/no-executor framing from the start (the only
   "executor" mention is the accurate
   `worker._cross_fire_state_seam` "Dead code until … lands"
   docstring). Carried phase-12 non-binding 🔵 FIXED:
   `app/v2/tool_tags.py` `_ADMIN_APPROVAL_TAGS` comment no
   longer says "trigger the admin-approval friction gate" —
   it states the §5.9 advisory-WARNING SIGNAL reality (no
   gate/executor; conceptual/DEFERRED). The ONE
   `CONTRACTS_V2_DESIGN.md` reconciliation applied: §6.5 LIVE
   note (primitives over the byte-untouched phase-3 CAS;
   "locks"=CAS+existing single-flight claim, NO new lock —
   Q2; per-schedule D5, shared-namespace DEFERRED — Q4) +
   §12 step-13 shipped-scope (primitives + build-the-layer
   DEAD seam, NO executor — Q1a). design ≡ plan ≡ code ≡
   tag. 5→13 invariant byte-proof vs `v2-phase-12-complete`:
   `app/v2/sources/`, `app/v2/emit/`, `binding.py`,
   `storage/schedule_state.py`, `ddl/`, `models/state.py`
   EMPTY diff. Annotated tag `v2-phase-13-complete` on the
   final commit, NOT pushed (gated on the claude-reviewer
   CLOSEOUT PASS).

## 5. Test inventory (highlights)

- `test_runtime_state.py` — `state_write` first write →
  version 1; CAS hit (correct `expected_version`) → bump;
  CAS stale (wrong `expected_version`) → `stale_version`, no
  mutation; `state_read` shape + `None`;
  `StateRunMismatchError` on cross-schedule lineage; NO
  internal retry (two racing writers — second sees stale,
  caller decides).
- Seam test — guard reachable + delegates to the primitives;
  AST pin the live fire path never consults it; phase-11/12
  boundaries byte/behaviour-unchanged (regression pins
  green).
- `test_phase13_import_hygiene.py` — alias-robust no
  `uuid.uuid4`/`datetime.now`/vendor-SDK module-load.

## 6. CI guard checks

- `check_phase_scope.py --staged` and
  `--diff v2-phase-12-complete` exit 0; `PHASE_ALLOWLIST[13]`
  = exactly the phase-13 surface (no agent mount — mirrors
  phase-10/12 build-the-layer).
- Alias-robust AST import-hygiene pin (phase-11/12
  carry-forward).
- `phase >= 9` doc-coupling (`ORI_SKIP_DOC_CHECK` is
  harness-DENIED — never bypass).
- `gen_docs` regen at closeout; stage INDEX/AGENTS_INVENTORY
  only if `files_changed != 0`.
- Full `tests/v2` green, no regression (2357 baseline +
  phase-13 adds).

## 7. Acceptance criteria for `v2-phase-13-complete`
(provisional — finalised after round-1 disposition)

1. Branch ahead of `v2-phase-12-complete` by N small
   per-slice commits.
2. `state_write` first write → version 1; CAS-correct
   `expected_version` bumps; CAS-stale refuses (no mutation),
   typed `stale_version`; NO internal retry (caller owns).
3. `state_read` returns the §6.5 shape (`value, version,
   written_at, written_by_run`) or `None`.
4. `StateRunMismatchError` propagates on cross-schedule
   lineage (audit-truth preserved).
5. The runtime primitives reuse the shipped
   `get_state`/`set_state_cas` — NO SQL/policy
   re-implementation; the phase-3 storage contract
   (caller-owns-retry, lineage cross-check,
   `NaiveDatetimeError`) intact.
6. Build-the-layer seam reachable-but-deferred; the live
   fire path never consults it (AST pin); phase-11 worker
   boundary + phase-12 reasoning enforcement
   byte/behaviour-unchanged (regression pins green); NO
   stateful-flow executor shipped.
7. §11.1: OneOff / v1 / phase-9–12 fire path +
   emit/cache/resolver byte/behaviour-unchanged (5→13
   byte-proof vs `v2-phase-12-complete`).
8. No `datetime.now`/`uuid.uuid4`/vendor-SDK module-load in
   any phase-13 NEW module; alias-robust AST pin green.
9. Phase guard `--staged` + `--diff v2-phase-12-complete`
   exit 0.
10. Full `tests/v2` green; no regression; §7 walked with
    evidence.
11. plan ≡ shipped code ≡ design ≡ tag annotation, zero
    divergence (one reconciliation pass at closeout;
    semantic-intent, not literal-token). The carried
    phase-12 non-binding 🔵 fixed in this closeout sweep.
12. Annotated tag `v2-phase-13-complete` created (push gated
    on reviewer CLOSEOUT PASS).

## 8. Tag annotation (draft — finalised at closeout)

```
v2 phase 13 complete

Cross-fire state primitives (§12 step 13). The §6.5
runtime primitives state_read / state_write are wired over
the shipped phase-3 storage CAS (get_state /
set_state_cas): state_write does an atomic
compare-version-and-set when expected_version is given
(typed written / stale_version outcome; NO internal retry —
caller owns it), state_read returns
{value, version, written_at, written_by_run} or None.
StateRunMismatchError lineage audit-truth preserved.
"locks + CAS" = the version-CAS plus the EXISTING
per-schedule single-flight run claim — NO new lock
primitive. schedule_state stays per-schedule (D5);
cross-schedule shared namespace is a deferred design
open-question, NOT shipped. A build-the-layer worker/flow
seam is wired where a future deterministic state-loader
would state_read -> pick -> state_write, but is DEAD: no
stateful-flow / reasoning EXECUTOR is shipped (no §12 step
owns it), so the phase-11 _fail_run boundary + phase-12
read-only-reasoning enforcement are byte/behaviour-unchanged
and the seam is never consulted on the live fire path.
OneOff / v1 / the phase-9–12 fire path + emit/cache/resolver
are byte/behaviour-unchanged (§11.1 additive).

NOT shipped (deferred): the deterministic stateful-flow /
reasoning executor + stateful RecurringSeriesFromSource
progress; cross-schedule shared state namespace; a new
advisory lock primitive; idempotency/cancellation (step
14); observability (15); migration tooling (16).

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 13, §6.5, D5
Plan:   docs/PHASE_13_PLAN.md
```

## 9. claude-reviewer round-1 disposition (CLOSED — Q1–Q6 ALL RATIFIED)

Plan-review round 1 on `121c814`: PASS — plan APPROVED,
Q1–Q6 ALL recommended dispositions RATIFIED. Baked here
verbatim (the phase-10/11/12 disposition-log discipline) so a
future drift is caught against the decision, not
re-litigated.

- **Q1 = (a).** Build-the-layer §6.5 primitives + a DEAD
  worker/flow seam; NO stateful-flow / reasoning executor
  (no §12 step owns it). Static primitives ARE the
  deliverable.
- **Q2.** "locks" = the EXISTING `version` CAS + the
  EXISTING per-schedule single-flight run claim — NO new
  lock primitive/table.
- **Q3.** Typed `written` / `stale_version` outcome;
  caller-owns-retry (NO internal loop/spin);
  `StateRunMismatchError` PROPAGATES (lineage audit-truth).
- **Q4.** `schedule_state` per-schedule (D5); cross-schedule
  shared-namespace OUT of scope / DEFERRED (design open
  question, not a step-13 deliverable).
- **Q5.** New module `app/v2/runtime/state.py` (storage CAS
  stays a pure storage primitive; the runtime-facing layer
  sits on top).
- **Q6.** §11.1 additive — NO existing fire path consumes
  cross-fire state; OneOff / v1 / phase-9–12 fire path +
  emit/cache/resolver byte/behaviour-unchanged.

Phase-12 non-binding 🔵 confirmed on this phase's closeout
sweep (FIXED — `app/v2/tool_tags.py` `_ADMIN_APPROVAL_TAGS`
comment terminology).

## 10. Hard rules (carried forward from phases 9–12)

- Slice-gated; pause after each commit for claude-reviewer;
  no push without a CLOSEOUT PASS.
- Invariants phase 13 must NOT regress: §3.5 typed-error
  taxonomy + `fallback_eligible`; resolver
  exactly-one-terminal-outcome / never-raise (UNTOUCHED);
  cache `ttl>0` no-probe / `==0` probe-every-fire
  (UNTOUCHED); §5.3.1 dirfd fence (UNTOUCHED); the phase-11
  worker pure-snapshot-consumer + `_fail_run` boundary
  (UNTOUCHED); phase-12 read-only-reasoning enforcement +
  build-the-layer guard (UNTOUCHED — only its boundary
  message, never the code, may ever change and not here);
  §11.1 additive cutover (OneOff / v1 / phase-9–12
  byte/behaviour-unchanged); the phase-3
  `get_state`/`set_state_cas` storage contract
  (caller-owns-retry, lineage cross-check,
  `NaiveDatetimeError`) preserved; `tool_tags.py`
  pure-contract preserved.
- **Carried phase-12 non-binding 🔵 (fix in this phase's
  closeout sweep):** `app/v2/tool_tags.py` `_ADMIN_APPROVAL_TAGS`
  comment "trigger the admin-approval friction gate" →
  "§5.9 advisory friction (signal; the gate is conceptual /
  deferred)".
- `uv run python …` always; no `sed`; refresh
  `.docs_read_marker` via
  `echo "yes" | uv run python scripts/check_docs_read.py`
  before each commit; commit trailer
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- Leave `app/tools/youtube.py` dirty + `.playwright-mcp/` +
  `scripts/{amazon_ads_mcp_proxy,diag_gemini_caching,diag_removal_order}.py`
  untracked — never stage.
- plan ≡ design ≡ code ≡ tag, semantic-intent reconciliation
  (the phase-9/10/11/12 stale-wording lesson — not just the
  literal-token set); design doc amended only in the ONE
  closeout reconciliation pass.
