# Phase 14 plan — §12 step 14: idempotency + cancellation

Status: **DRAFT for claude-reviewer plan-review round 1.** Not
pushed. Same loop contract as phases 5–13: plan-review rounds
→ per-slice implement + review → closeout (full v2 suite +
acceptance walk + phase guard + gen_docs + repo-wide
semantic-intent sweep + the ONE `docs/CONTRACTS_V2_DESIGN.md`
reconciliation pass + annotated tag `v2-phase-14-complete`,
tag push gated on a claude-reviewer CLOSEOUT PASS).

`docs/CONTRACTS_V2_DESIGN.md` §12 step 14 is the canonical
scope source:

> 14. **Idempotency + cancellation**: per-emit idempotency
> keys; paused/archived enforcement; `paused_pending_policy`.

Dependency (design §12): *step 14 depends on 4, 12, 13*
(runtime/worker + enforcement + cross-fire state — all
shipped).

Design references: §6.4 (emit idempotency key), §6.x
ledger/cancellation table (RunStatus `cancelled`,
`run_cancelled`, `emit_skipped_idempotent`), §7 Pause/Archive
+ `paused_pending_policy`, the state-machine cancellation
transitions.

---

## 0. Scope refinement + open design questions (round-1 input)

### 0.0 What already exists (do NOT rebuild)

- `app/v2/idempotency.py` — PURE `compute_idempotency_key(*,
  schedule_id, root_run_id, emit_id)` →
  `"{schedule_id}:{root_run_id}:{emit_id}"` (validated; no
  I/O). Docstring: *"runtime adapters call it at phase 4+ when
  they actually fire emits"* — they DO NOT yet; **no emit
  idempotency dedup is wired anywhere** (slack_reminder.py /
  source_post.py carry no idempotency code).
- v001 DDL `events.kind` CHECK ALREADY includes
  `emit_skipped_idempotent` and `run_cancelled`; `runs.status`
  CHECK already includes `cancelled`. **No v002 / DDL change
  is needed** (contrast phase-11 Option-B).
- State machine: `cancelled` is terminal; the
  pending/non-terminal → `cancelled` transitions + the
  `run_cancelled` event are spec'd.
- `app/v2/authoring/lifecycle.py` `schedule_archive` ALREADY
  cancels pending Runs (`update_status_with_event(...,
  cancel_pending_runs=True)`); `schedule_pause` is currently
  a no-op for already-pending Runs (the
  `paused_pending_policy` was explicitly DEFERRED — see
  `runtime/lifecycle.py` "later phase").
- **Missing (= phase-14 deliverable):** (i) the emit
  idempotency DEDUP (ledger pre-check + `emit_skipped_idempotent`),
  (ii) the `paused_pending_policy` enum + ScheduleSpec field +
  pause enforcement, (iii) pause(`cancel_pending`)
  cancellation parity with archive.

### 0.1 Open design questions for round 1

Each carries a RECOMMENDED disposition (reviewer ratifies or
overrides — the phase-11/12/13 §0 pattern). Nothing below is
unilaterally decided.

- **Q1 — idempotency-dedup placement (the central question;
  interacts with the phases-9–13 "emit adapters
  byte-untouched" invariant).** A retry/recovery re-run of a
  logical fire would double-deliver without dedup — unlike
  phases 12/13 there IS a LIVE consumer (the recovery/retry
  path), so a build-the-layer-DEAD treatment would leave the
  documented double-delivery risk unaddressed. Options:
  - **(a) LIVE pre-emit check in the WORKER emit branch —
    RECOMMENDED.** A pure dedup-decision helper (extend
    `idempotency.py`: a ledger-query
    `prior_emit_succeeded(conn, key) -> bool`) + an ADDITIVE
    pre-emit check in `Worker._dispatch_emit_branch` BEFORE
    the adapter call: on a prior-success hit, write
    `emit_skipped_idempotent` + treat as success, skip the
    adapter. **The emit ADAPTERS (slack_reminder.py /
    source_post.py) stay BYTE-UNTOUCHED** — the empty-diff
    invariant from phases 9–13 holds; dedup is the worker's
    concern, not the adapter's. Native upstream tokens (Slack
    `client_msg_id`) are out of scope here (defense-in-depth
    extra, deferred).
  - (b) dedup inside the emit adapter — BREAKS the
    emit-byte-untouched invariant; needs an explicit reviewer
    relaxation (phase-11 §0.3-style).
  - (c) build-the-layer DEAD — REJECTED: a live retry
    consumer exists; deferring contradicts step-14's purpose.
  - *Rationale for (a):* makes dedup LIVE where the risk
    actually is (retry) while preserving the emit-adapter
    byte-untouched invariant via an additive worker-branch
    check.
- **Q2 — `paused_pending_policy`.** Add a `PausedPendingPolicy`
  enum (`let_complete` / `cancel_pending`) + an OPTIONAL
  ScheduleSpec field. RECOMMENDED default = **`let_complete`**
  (a pause must not silently kill in-flight pending work
  unless explicitly asked — least-surprise; matches the
  archive-vs-pause asymmetry in design §7). Pause then:
  `let_complete` → already-pending Runs untouched (current
  behaviour); `cancel_pending` → cancel them (parity with
  archive).
- **Q3 — canonical-hash stability of the new spec field.**
  The field is OPTIONAL + defaulted; per the phase-9
  `TemplateRef.args` precedent (design §5.2), a spec that
  does NOT set it round-trips with NO `compute_hash` drift.
  RECOMMENDED: include it in the canonical body ONLY when
  set (or default-elided) so every pre-amendment frozen spec
  hashes unchanged. Pinned by a hash-stability regression
  test. NO DDL change (spec is JSON in `schedules`).
- **Q4 — cancellation reuse.** RECOMMENDED: reuse the
  EXISTING `run_cancelled` EventKind + `cancelled` RunStatus
  + the state-machine transitions + the
  `update_status_with_event(cancel_pending_runs=True)` seam
  that `schedule_archive` already uses. NO new event kind, NO
  v002 (the kinds are already in the v001 CHECK).
- **Q5 — §11.1 + emit byte-untouched.** RECOMMENDED: with
  Q1(a), `slack_reminder.py` / `source_post.py` /
  `sources/` / `cache.py` / `resolver.py` stay EMPTY-diff vs
  `v2-phase-13-complete`; OneOff / v1 / the phase-9–13 fire
  path are behaviour-unchanged EXCEPT the intended new
  dedup-skip (which is itself a success path — no
  double-delivery). Pinned.
- **Q6 — dedup-decision module placement.** RECOMMENDED:
  extend `app/v2/idempotency.py` (cohesive: the key + the
  ledger dedup-decision together, both pure / DI-conn). NO
  SQL re-implemented — the ledger query uses the shipped
  `app/v2/storage/events.py` read surface.
- **Q7 — paused_pending_policy enforcement site.**
  RECOMMENDED: in the pause lifecycle path
  (`authoring/lifecycle.py` `schedule_pause` /
  `runtime/lifecycle.py`), reusing the archive
  cancel-pending seam — NOT a new wakeup/worker branch.

---

## 1. Scope statement

### In scope (phase 14), assuming Q1(a) / Q2–Q7 RECOMMENDED
(reviewer may revise):

1. **Idempotency dedup (LIVE, worker branch).** Pure
   `prior_emit_succeeded(conn, *, idempotency_key) -> bool`
   (extend `idempotency.py`; reuses `storage/events.py`,
   no SQL reimpl). An ADDITIVE pre-emit check in
   `Worker._dispatch_emit_branch`: compute the key
   (`compute_idempotency_key`), query the ledger; on a prior
   `emit_succeeded` hit → write `emit_skipped_idempotent` +
   succeed WITHOUT calling the adapter. Emit adapters
   BYTE-UNTOUCHED.
2. **`paused_pending_policy`.** New `PausedPendingPolicy`
   enum (`let_complete` / `cancel_pending`) + optional
   ScheduleSpec field (default `let_complete`,
   hash-stable — Q3). `schedule_pause` honours it
   (`cancel_pending` → cancel already-pending Runs via the
   existing archive seam; `let_complete` → current no-op).
3. **Cancellation parity.** Pause(`cancel_pending`) emits
   `run_cancelled` for already-pending Runs exactly as
   `schedule_archive` does (reused seam; no new kind).
4. **Hygiene + docs.** Alias-robust AST import-hygiene pin
   for any new module. `PHASE_ALLOWLIST[14]` +
   `.v2-current-phase`→14 land in the plan commit (this
   commit). `gen_docs` regen + stage at closeout.

### Out of scope (phase 14) — deferred with rationale

- **Native upstream dedup tokens** (Slack `client_msg_id`
  pass-through) — defense-in-depth extra, not the core
  ledger dedup; deferred.
- **The retry CHAIN itself** (scheduling new attempts) — the
  idempotency dedup protects an already-existing retry path;
  authoring/scheduling the retry policy is its own concern.
- **Reasoning/stateful-flow executor** — still no §12 step
  owns it (carried from phases 11–13).
- Observability (step 15), migration tooling (16).

---

## 2. New file paths (provisional — finalised per slice)

- Extensions only (no new module expected): `app/v2/idempotency.py`
  (+ `prior_emit_succeeded` ledger-query, pure/DI-conn),
  `app/v2/enums.py` (+ `PausedPendingPolicy`),
  `app/v2/models/schedule.py` (+ optional
  `paused_pending_policy` field, hash-stable),
  `app/v2/runtime/worker.py` (additive pre-emit dedup check),
  `app/v2/authoring/lifecycle.py` / `app/v2/runtime/lifecycle.py`
  (pause honours the policy).
- `tests/v2/test_idempotency_dedup.py`,
  `tests/v2/test_paused_pending_policy.py`,
  `tests/v2/test_phase14_import_hygiene.py` (if a new module
  appears — else fold the hygiene assertion into an existing
  pin).

## 3. Module APIs (sketch — finalised per slice)

- `prior_emit_succeeded(conn, *, idempotency_key: str) ->
  bool` — True iff the ledger has an `emit_succeeded` event
  whose payload carries this key. Pure read.
- `PausedPendingPolicy(str, Enum)`: `LET_COMPLETE` /
  `CANCEL_PENDING`.
- `ScheduleSpec.paused_pending_policy: PausedPendingPolicy =
  LET_COMPLETE` (optional, hash-stable per Q3).
- Worker pre-emit: compute key → `prior_emit_succeeded` →
  branch (skip+`emit_skipped_idempotent` vs proceed).

## 4. Slice ordering + commit cadence (draft — reviewer
finalises)

0. **plan + phase transition** (this commit; NOT pushed) —
   `docs/PHASE_14_PLAN.md`, `PHASE_ALLOWLIST[14]`,
   `.v2-current-phase`→14. Design doc NOT touched here.
1. **Idempotency dedup-decision** (pure
   `prior_emit_succeeded` over `storage/events.py`, no SQL
   reimpl) + unit tests + hygiene assertion.
2. **LIVE worker pre-emit dedup** — additive check in
   `_dispatch_emit_branch`; `emit_skipped_idempotent` on hit;
   emit adapters BYTE-UNTOUCHED (empty-diff pinned);
   phase-9–13 fire-path regression pins green.
3. **`paused_pending_policy`** — enum + optional hash-stable
   ScheduleSpec field (hash-stability regression pin) +
   `schedule_pause` honours it (reuse the archive
   cancel-pending seam; `run_cancelled`, no new kind).
4. **closeout** — full `tests/v2`, acceptance walk, phase
   guards, `gen_docs` regen+stage, REPO-WIDE semantic-intent
   sweep (the phase-9/10/11/12/13 lesson), the ONE
   `docs/CONTRACTS_V2_DESIGN.md` reconciliation pass (§6.4 /
   §7 LIVE notes: dedup wired in the worker branch, emit
   adapters byte-untouched; `paused_pending_policy` shipped;
   cancellation parity), annotated tag `v2-phase-14-complete`
   (gated on CLOSEOUT PASS).

## 5. Test inventory (highlights)

- `test_idempotency_dedup.py` — `prior_emit_succeeded` True
  iff a prior `emit_succeeded` with the key; False otherwise;
  pure read, no mutation.
- worker dedup — first fire delivers; a retry/recovery re-run
  with the same `root_run_id`+`emit_id` ⇒
  `emit_skipped_idempotent`, NO second adapter call, run still
  succeeds; distinct schedules/emit_ids never collide.
- `test_paused_pending_policy.py` — `let_complete` (default)
  pause leaves pending Runs; `cancel_pending` pause emits
  `run_cancelled` for them (parity with archive); hash
  stability: a spec without the field hashes identically to
  pre-amendment.
- emit-adapter byte-untouched empty-diff proof; phase-9–13
  fire-path/boundary/seam regression pins green.

## 6. CI guard checks

- `check_phase_scope.py --staged` and
  `--diff v2-phase-13-complete` exit 0; `PHASE_ALLOWLIST[14]`
  = exactly the phase-14 surface (no agent mount).
- Alias-robust AST import-hygiene (phase-11/12/13
  carry-forward).
- `phase >= 9` doc-coupling (`ORI_SKIP_DOC_CHECK`
  harness-DENIED).
- `gen_docs` regen at closeout; stage INDEX/AGENTS_INVENTORY
  only if `files_changed != 0`.
- Full `tests/v2` green, no regression (2383 baseline +
  phase-14 adds).

## 7. Acceptance criteria for `v2-phase-14-complete`
(provisional — finalised after round-1 disposition)

1. Branch ahead of `v2-phase-13-complete` by N small
   per-slice commits.
2. `prior_emit_succeeded` True iff a prior `emit_succeeded`
   event carries the key; pure read.
3. A retry/recovery re-run of the same logical fire
   (`root_run_id`+`emit_id` stable) ⇒ `emit_skipped_idempotent`,
   NO duplicate adapter delivery, run succeeds; distinct
   keys never collide.
4. Emit adapters (`slack_reminder.py`/`source_post.py`) +
   `sources/`/`cache.py`/`resolver.py` EMPTY-diff vs
   `v2-phase-13-complete` (dedup is a worker-branch concern).
5. `PausedPendingPolicy` enum + optional hash-stable
   ScheduleSpec field; default `let_complete`; a spec without
   the field hashes identically to pre-amendment.
6. `schedule_pause` honours the policy: `let_complete` leaves
   pending Runs; `cancel_pending` emits `run_cancelled`
   (parity with `schedule_archive`); no new EventKind, no
   v002.
7. Phase-11 `_fail_run` boundary + phase-12 enforcement +
   phase-13 state seam byte/behaviour-unchanged; no executor.
8. No `datetime.now`/`uuid.uuid4`/vendor-SDK module-load in
   any phase-14 NEW module; alias-robust AST pin green.
9. Phase guard `--staged` + `--diff v2-phase-13-complete`
   exit 0.
10. Full `tests/v2` green; no regression; walked with
    evidence.
11. plan ≡ shipped code ≡ design ≡ tag annotation, zero
    divergence (one reconciliation pass at closeout;
    semantic-intent).
12. Annotated tag `v2-phase-14-complete` created (push gated
    on reviewer CLOSEOUT PASS).

## 8. Tag annotation (draft — finalised at closeout)

```
v2 phase 14 complete

Emit idempotency + cancellation (§12 step 14). The emit
idempotency DEDUP is wired LIVE: Worker._dispatch_emit_branch
computes the §6.4 key
(compute_idempotency_key = schedule_id:root_run_id:emit_id,
phase-1 helper) and, BEFORE calling the adapter, queries the
event ledger via prior_emit_succeeded — on a prior
emit_succeeded hit it writes emit_skipped_idempotent and
succeeds WITHOUT re-delivering (a retry/recovery re-run of
the same logical fire no longer double-delivers). The emit
ADAPTERS (slack_reminder.py / source_post.py) +
sources/cache/resolver are BYTE-UNTOUCHED — dedup is a
worker-branch concern (the phases-9–13 emit-byte-untouched
invariant holds). No v002 / DDL change:
emit_skipped_idempotent + run_cancelled + cancelled were
already in the v001 CHECK.

paused_pending_policy ships: a PausedPendingPolicy enum
(let_complete / cancel_pending) + an OPTIONAL hash-stable
ScheduleSpec field (default let_complete — a spec that does
not set it hashes identically to pre-amendment, the phase-9
TemplateRef.args precedent). schedule_pause honours it:
let_complete leaves already-pending Runs (prior behaviour),
cancel_pending emits run_cancelled for them — parity with
schedule_archive, reusing the existing cancel-pending seam,
NO new EventKind.

The phase-11 _fail_run reasoning boundary, phase-12
read-only-reasoning enforcement, and phase-13 cross-fire
state seam are byte/behaviour-unchanged; no reasoning /
stateful-flow executor is shipped (no §12 step owns it).
OneOff / v1 / the phase-9–13 fire path are
behaviour-unchanged except the intended dedup-skip (itself a
success, no double-delivery).

NOT shipped (deferred): native upstream dedup tokens (Slack
client_msg_id pass-through); the retry CHAIN scheduler; the
reasoning/stateful-flow executor; observability (step 15);
migration tooling (16).

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 14, §6.4, §7
Plan:   docs/PHASE_14_PLAN.md
```

## 9. claude-reviewer round-1 disposition (OPEN)

Q1–Q7 above await round-1 adjudication. Decisions baked here
verbatim (the phase-10/11/12/13 disposition-log discipline)
so a future drift is caught against the decision, not
re-litigated.

## 10. Hard rules (carried forward from phases 9–13)

- Slice-gated; pause after each commit for claude-reviewer;
  no push without a CLOSEOUT PASS.
- Invariants phase 14 must NOT regress: §3.5 typed-error
  taxonomy + `fallback_eligible`; resolver
  exactly-one-terminal-outcome / never-raise (UNTOUCHED);
  cache `ttl>0` no-probe / `==0` probe-every-fire
  (UNTOUCHED); §5.3.1 dirfd fence (UNTOUCHED); the phase-3
  `get_state`/`set_state_cas` CAS contract
  (caller-owns-retry, lineage cross-check) + the phase-13
  `state_read`/`state_write` primitives + `_cross_fire_state_seam`
  (UNTOUCHED); the phase-11 worker pure-snapshot-consumer +
  `_fail_run` reasoning boundary
  (`reasoning_unsupported_pending_step_12`, byte-identical
  code) (UNTOUCHED); the phase-12 read-only-reasoning
  enforcement (`_validate_reasoning_tool_mode` /
  `_validate_customflow_admin_friction` + their distinct
  codes; only an enforcement boundary MESSAGE may ever
  change, never the code, and not here) (UNTOUCHED); §11.1
  additive cutover — OneOff / v1 / phase-9–13 fire path +
  the emit adapters (`slack_reminder.py`/`source_post.py`)
  byte/behaviour-unchanged EXCEPT the intended dedup-skip
  success path; no reasoning / stateful-flow executor
  shipped (no §12 step owns it); `tool_tags.py`
  pure-contract preserved.
- `uv run python …` always; no `sed`; refresh
  `.docs_read_marker` via
  `echo "yes" | uv run python scripts/check_docs_read.py`
  before each commit; commit trailer
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- Leave `app/tools/youtube.py` dirty + `.playwright-mcp/` +
  `scripts/{amazon_ads_mcp_proxy,diag_gemini_caching,diag_removal_order}.py`
  untracked — never stage.
- plan ≡ design ≡ code ≡ tag, semantic-intent reconciliation
  (the phase-9/10/11/12/13 stale-wording lesson — not just
  the literal-token set); design doc amended only in the ONE
  closeout reconciliation pass.
