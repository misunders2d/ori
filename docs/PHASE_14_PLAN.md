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
- **The worker writes NO `emit_succeeded` today.** The
  success path returns `"succeeded"` from
  `_dispatch_emit_branch`; `_tick_with_conn` writes ONE
  `RUN_SUCCEEDED` event in the running→succeeded
  `update_run_status_and_append_event` terminal transaction.
  `EMIT_SUCCEEDED` is in the enum + v001 DDL CHECK but is
  NOT emitted anywhere. So dedup is **read-AND-write**: the
  keyed marker must also be WRITTEN, or the pre-check has
  nothing to find (vacuous) — see §0.1 Q1 / the round-1 🟡.
- **Missing (= phase-14 deliverable):** (i) the emit
  idempotency DEDUP — both the post-success keyed
  `emit_succeeded` WRITE and the pre-emit READ +
  `emit_skipped_idempotent`, (ii) the `paused_pending_policy`
  enum + ScheduleSpec field + pause enforcement, (iii)
  pause(`cancel_pending`) cancellation parity with archive.

### 0.1 Open design questions for round 1

Each carries a RECOMMENDED disposition (reviewer ratifies or
overrides — the phase-11/12/13 §0 pattern). Nothing below is
unilaterally decided.

- **Q1 — idempotency-dedup placement + the FULL read-AND-write
  protocol (the central question; round-1 🟡; interacts with
  the phases-9–13 "emit adapters byte-untouched" invariant).**
  Dedup is a READ-AND-WRITE protocol — specifying only the
  READ is VACUOUS (nothing writes the keyed marker, so a
  retry finds nothing) AND would over-claim exactly-once.
  Phase 14 is the FIRST LIVE consumer, so the full protocol
  must ship. A retry/recovery re-run would double-deliver
  without it — unlike phases 12/13 there IS a LIVE consumer,
  so build-the-layer-DEAD is REJECTED.
  - **(a) LIVE read-AND-write in the WORKER emit branch —
    RECOMMENDED (round-1 ratified).** The FULL protocol:
    1. **READ (pre-emit):** compute the §6.4 key
       (`compute_idempotency_key`); query the ledger via a
       pure `prior_emit_succeeded(conn, *, idempotency_key)`
       for a prior `emit_succeeded` event whose payload
       CARRIES THIS KEY (not any `emit_succeeded`). Hit ⇒
       skip the adapter, write `emit_skipped_idempotent`,
       succeed.
    2. **WRITE (post-success):** when the adapter delivered,
       the WORKER BRANCH writes the keyed `emit_succeeded`
       marker **ATOMIC in the run-terminal transaction**
       (the same `update_run_status_and_append_event`
       running→succeeded commit that writes `RUN_SUCCEEDED`)
       — both-or-neither, so the dedup record is durable iff
       the run terminal-commits (no orphan / no missing
       key).
    3. **HONEST residual window:** ledger dedup eliminates
       double-delivery for a retry that runs AFTER a DURABLY
       committed keyed `emit_succeeded`. The
       deliver-then-crash-BEFORE-the-terminal-commit window
       is the inherent **at-least-once** boundary (the
       adapter call is non-transactional external I/O; it is
       NOT atomic with the marker write) — mitigated only by
       the DEFERRED native upstream tokens. NOT exactly-once.
    The emit ADAPTERS (`slack_reminder.py` / `source_post.py`)
    stay BYTE-UNTOUCHED — both the read and the write are the
    worker's concern; the phases-9–13 empty-diff invariant
    holds. Native upstream tokens (Slack `client_msg_id`)
    out of scope (deferred).
  - (b) dedup inside the emit adapter — BREAKS the
    emit-byte-untouched invariant; rejected.
  - (c) build-the-layer DEAD — REJECTED: a live retry
    consumer exists; deferring contradicts step-14's purpose.
  - *Rationale for (a):* makes the FULL dedup protocol LIVE
    where the risk actually is (retry) while preserving the
    emit-adapter byte-untouched invariant via an additive
    worker-branch read + a terminal-TX-atomic write, and
    states the protected window honestly (no exactly-once
    over-claim — the phase-9–13 over-claim/stale-wording
    lesson, written accurately the first time).
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
  path are behaviour-unchanged EXCEPT (i) the new
  post-success keyed `emit_succeeded` marker written atomic
  in the existing run-terminal TX and (ii) the intended
  dedup-skip (a retry that finds a durable prior keyed
  success ⇒ `emit_skipped_idempotent`, itself a success).
  This protects a retry AFTER a durable success; it is NOT
  exactly-once (the deliver-then-crash-before-commit window
  is the inherent at-least-once boundary). Pinned.
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

1. **Idempotency dedup — the FULL read-AND-write protocol
   (LIVE, worker branch).**
   - READ: pure `prior_emit_succeeded(conn, *,
     idempotency_key) -> bool` (extend `idempotency.py`;
     reuses `storage/events.py`, no SQL reimpl; matches
     `emit_succeeded` kind AND payload-carries-the-key, not
     any `emit_succeeded`). ADDITIVE pre-emit check in
     `Worker._dispatch_emit_branch`: compute the key
     (`compute_idempotency_key`), query; on a prior keyed
     hit → write `emit_skipped_idempotent` + succeed WITHOUT
     calling the adapter.
   - WRITE: on a real delivery, the worker branch writes the
     keyed `emit_succeeded` marker ATOMIC in the run-terminal
     transaction (the running→succeeded
     `update_run_status_and_append_event` commit) —
     both-or-neither with `RUN_SUCCEEDED`; durable iff the
     run terminal-commits (no orphan / no missing key).
   - PROTECTED-WINDOW scope (honest, not exactly-once):
     dedup eliminates double-delivery for a retry AFTER a
     durably committed keyed `emit_succeeded`; the
     deliver-then-crash-before-terminal-commit window is the
     inherent at-least-once boundary (deferred upstream
     tokens are the only mitigation). Emit adapters
     BYTE-UNTOUCHED (both read and write are the worker's
     concern).
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
  whose payload CARRIES THIS KEY (not any `emit_succeeded`).
  Pure read, no mutation.
- Worker WRITE side: on real delivery, append a keyed
  `emit_succeeded` Event (payload carries `idempotency_key`)
  ATOMIC in the run-terminal transaction (the same
  `update_run_status_and_append_event` running→succeeded
  commit as `RUN_SUCCEEDED`) — both-or-neither.
- `PausedPendingPolicy(str, Enum)`: `LET_COMPLETE` /
  `CANCEL_PENDING`.
- `ScheduleSpec.paused_pending_policy: PausedPendingPolicy =
  LET_COMPLETE` (optional; ELIDED from the `compute_hash`
  canonical body when unset/default — Q3, no hash drift).
- Worker pre-emit READ: compute key → `prior_emit_succeeded`
  → branch (skip+`emit_skipped_idempotent` vs proceed);
  post-success WRITE the keyed marker in the terminal TX.

## 4. Slice ordering + commit cadence (draft — reviewer
finalises)

0. **plan + phase transition** (this commit; NOT pushed) —
   `docs/PHASE_14_PLAN.md`, `PHASE_ALLOWLIST[14]`,
   `.v2-current-phase`→14. Design doc NOT touched here.
1. **Idempotency dedup-decision (READ side)** — pure
   `prior_emit_succeeded` over `storage/events.py` (no SQL
   reimpl; matches `emit_succeeded` kind AND
   payload-carries-the-key) + unit tests + hygiene
   assertion.
   **LANDED** — `app/v2/idempotency.py` EXTEND-ONLY:
   `prior_emit_succeeded(conn, *, idempotency_key) -> bool`
   delegates to the SHIPPED
   `storage/events.py::get_last_emit_succeeded` (`kind =
   'emit_succeeded' AND json_extract(payload_json,
   '$.idempotency_key') = ?`) — NO SQL re-implemented, pure
   READ, no mutation. Collision-safe (test-pinned): an
   `emit_failed` / `run_succeeded` carrying the same key, OR
   an `emit_succeeded` carrying a DIFFERENT key, NEVER
   matches — only the exact (kind, key) pair. `compute_idempotency_key`
   BYTE-UNCHANGED (extend-only — diff has zero deletions).
   `tests/v2/test_idempotency_dedup.py` (True/False/
   distinct-never-match/kind-collision-safe/pure-no-mutation)
   + a FOLDED alias-robust import-hygiene assertion for
   `app.v2.idempotency` (no new module ⇒ folded here per the
   slice-1 hard-check, not a new phase-14 hygiene file).
   ZERO cross-phase touch: `emit/`, `sources/`, `worker.py`,
   `storage/`, `ddl/` EMPTY diff vs `v2-phase-13-complete`.
   `PHASE_ALLOWLIST[14]` unchanged (no new surface path).
2. **LIVE worker read-AND-write dedup** — (READ) additive
   pre-emit check in `_dispatch_emit_branch`,
   `emit_skipped_idempotent` on a durable prior keyed hit;
   (WRITE) post-success keyed `emit_succeeded` marker ATOMIC
   in the run-terminal TX (both-or-neither with
   `RUN_SUCCEEDED`). Emit adapters BYTE-UNTOUCHED (empty-diff
   pinned); phase-9–13 fire-path/boundary/seam regression
   pins green; the deliver-then-crash-before-commit
   at-least-once residual window pinned (no marker ⇒ a
   retry re-delivers — the inherent boundary, NOT a bug).
   **LANDED** (fork verdict Q-A=(a) / Q-B; §9.1). EmitStep-
   keyed (§6.4) on the source `source_post` step only —
   OneOff is OUT by construction (no EmitStep, no §6.4 key;
   A.1/A.2). `_dispatch_emit_branch` → `tuple[str,
   Optional[Event]]`: pre-emit `compute_idempotency_key` +
   `prior_emit_succeeded` (source-driven block only) ⇒ a
   durable prior keyed hit returns
   `("succeeded_idempotent_skip", emit_skipped_idempotent)`
   with NO adapter call / NO resolve; a real delivery returns
   `("succeeded", keyed emit_succeeded)`; skip_unchanged →
   `("succeeded_skipped", None)` (phase-11 path UNCHANGED, no
   marker); OneOff → `("succeeded", None)`. New
   `_commit_success_atomic` (B.1 structural mirror of
   `_commit_failure_atomic`) writes the optional marker + the
   running→succeeded UPDATE + `RUN_SUCCEEDED` in ONE
   `transaction(conn)`; the universal terminal-success path
   reroutes through it (`emit_marker_event=None` ⇒
   byte-behaviour-identical, B.2). Storage byte-untouched
   (B.4 — `update_run_status_and_append_event` NOT modified;
   emit/sources/cache/resolver/ddl empty-diff vs
   `v2-phase-13-complete`). Coupled-test reconcile (NOT a
   mask): the direct-`_dispatch_emit_branch` assertions in
   `test_runtime_worker_emit_branch.py` /
   `test_runtime_source_fire.py` unpack the new tuple
   (`outcome, _marker = await …`) — behaviour byte-identical,
   only the return shape changed (ratified Q-B). New
   `test_idempotency_worker_dedup.py` (B.3 matrix +
   `_commit_success_atomic` both-or-neither B.1/B.6 + the
   structural A.2 pin).
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
  iff a prior `emit_succeeded` whose payload CARRIES THE KEY
  (False for an `emit_succeeded` with a different/absent
  key — NOT any `emit_succeeded`); pure read, no mutation.
- worker read-AND-write — first fire delivers AND writes the
  keyed `emit_succeeded` marker ATOMIC with `RUN_SUCCEEDED`
  in the run-terminal TX; a retry/recovery re-run with the
  same `root_run_id`+`emit_id` finds it ⇒
  `emit_skipped_idempotent`, NO second adapter call, run
  still succeeds; distinct schedules/emit_ids never collide.
- residual-window pin (honest, not exactly-once): when the
  terminal TX did NOT commit (deliver-then-crash-before-
  record), NO keyed marker exists ⇒ a retry re-delivers —
  the inherent at-least-once boundary, asserted as
  EXPECTED, not a bug.
- terminal-TX atomicity pin: the keyed `emit_succeeded` is
  durable IFF the run terminal-commits (both-or-neither with
  `RUN_SUCCEEDED` — no orphan marker, no missing key).
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
   event whose payload CARRIES THE KEY (not any
   `emit_succeeded`); pure read, no mutation.
3. The FULL protocol: a real delivery WRITES the keyed
   `emit_succeeded` marker ATOMIC in the run-terminal TX
   (both-or-neither with `RUN_SUCCEEDED`). A retry/recovery
   re-run of the same logical fire (`root_run_id`+`emit_id`
   stable) that finds a DURABLE prior keyed success ⇒
   `emit_skipped_idempotent`, NO duplicate adapter delivery,
   run succeeds; distinct keys never collide. This protects
   a retry AFTER a durable success — it is explicitly NOT
   exactly-once: the deliver-then-crash-before-terminal-commit
   window is the inherent at-least-once boundary (pinned as
   EXPECTED; mitigated only by the deferred upstream
   tokens). No §1/§7/§8 wording claims exactly-once or
   unconditional "no double-delivery".
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
idempotency DEDUP is wired LIVE as the FULL read-AND-write
protocol in Worker._dispatch_emit_branch (the emit ADAPTERS
slack_reminder.py / source_post.py + sources/cache/resolver
are BYTE-UNTOUCHED — dedup is a worker-branch concern; the
phases-9–13 emit-byte-untouched invariant holds):
SCOPE (A.1, honest — NOT all emits are deduped): the §6.4
key is EmitStep-keyed, so dedup covers ONLY the source-driven
ExecutionPlan's source_post EmitStep. OneOff is template-emit
with no EmitStep ⇒ no §6.4 key ⇒ OUT of emit-dedup by
construction (a OneOff fire computes no key, consults no
prior marker, writes no keyed marker — the
emit_marker_event=None path, behaviour-identical to
pre-phase-14; its retry/recovery at-most-once is the
recovery/claim concern, not step-14). A OneOff sentinel key
is a deferred separate design question, NOT shipped.
- READ (pre-emit, source path only): compute the §6.4 key
  (compute_idempotency_key = schedule_id:root_run_id:emit_id,
  phase-1 helper); prior_emit_succeeded queries the ledger
  for a prior emit_succeeded whose payload CARRIES THIS KEY
  (not any emit_succeeded). A hit ⇒ write
  emit_skipped_idempotent + succeed WITHOUT calling the
  adapter.
- WRITE (post-success): on a real delivery the worker writes
  the keyed emit_succeeded marker ATOMIC in the run-terminal
  transaction (both-or-neither with RUN_SUCCEEDED) — durable
  iff the run terminal-commits (no orphan, no missing key).
This protects a retry that runs AFTER a DURABLY committed
keyed emit_succeeded. It is NOT exactly-once: the
deliver-then-crash-before-the-terminal-commit window is the
inherent at-least-once boundary (the adapter call is
non-transactional external I/O, not atomic with the marker);
the only mitigation is the DEFERRED native upstream tokens.
No v002 / DDL change: emit_succeeded + emit_skipped_idempotent
+ run_cancelled + cancelled were already in the v001 CHECK.

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
behaviour-unchanged except (i) the new post-success keyed
emit_succeeded marker (atomic in the existing run-terminal
TX) and (ii) the intended dedup-skip of a retry that finds a
durable prior keyed success — at-least-once with
post-durable-success dedup, NOT exactly-once.

NOT shipped (deferred): native upstream dedup tokens (Slack
client_msg_id pass-through); the retry CHAIN scheduler; the
reasoning/stateful-flow executor; observability (step 15);
migration tooling (16).

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 14, §6.4, §7
Plan:   docs/PHASE_14_PLAN.md
```

## 9. claude-reviewer round-1 disposition (CLOSED — Q1–Q7 ALL RATIFIED)

Round 1 on `b62b35c`: 🟡 HOLD — Q1–Q7 ALL recommended
dispositions RATIFIED; one HOLD-class 🟡 (the read-AND-write
protocol + honest residual-window — fixed in this round-2
revision: §0.1 Q1, §1, §3, §5, §7, §8). No carried items.
Baked here verbatim (the phase-10/11/12/13 disposition-log
discipline) so a future drift is caught against the decision,
not re-litigated.

- **Q1 = (a).** LIVE pre-emit dedup in `_dispatch_emit_branch`
  (adapters byte-untouched) — correct placement (dedup live
  where the retry risk is). (b) breaks emit-byte-untouched
  (rejected); (c) build-the-layer-DEAD correctly REJECTED (a
  live retry consumer exists — the correct departure from
  phases 11–13). **CONDITION (the 🟡):** the FULL
  read-AND-write protocol — pre-emit READ + post-success
  keyed `emit_succeeded` WRITE atomic in the run-terminal TX;
  honest protected-window scope (NOT exactly-once).
- **Q2 = ratified.** `PausedPendingPolicy{let_complete,
  cancel_pending}`, default `let_complete` (design §7
  archive-vs-pause asymmetry; a pause must not silently kill
  in-flight unless asked).
- **Q3 = ratified.** Optional hash-stable field (phase-9
  `TemplateRef.args` precedent, no DDL). **CONDITION:** the
  default MUST be ELIDED from the `compute_hash` canonical
  body when unset/default (NOT serialized — no hash drift).
- **Q4 = ratified.** Reuse `run_cancelled` / `cancelled` /
  state-machine PENDING→CANCELLED /
  `update_status_with_event(cancel_pending_runs=True)` — all
  shipped. NO new kind, NO v002.
- **Q5 = ratified.** Q1(a) ⇒ `slack_reminder.py` /
  `source_post.py` / `sources/` / `cache.py` / `resolver.py`
  EMPTY-diff vs `v2-phase-13-complete`. **CONDITION:**
  slice-2 empty-diff proof + phase-9–13
  fire-path/boundary/seam regression pins.
- **Q6 = ratified.** Extend `idempotency.py` (key +
  dedup-decision cohesive, pure/DI-conn). **CONDITION:** NO
  SQL reimpl — use the shipped `storage/events.py` read
  surface; pure read (no mutation); the query matches
  `emit_succeeded` kind AND payload-carries-the-key (NOT any
  `emit_succeeded`).
- **Q7 = ratified.** `schedule_pause` honours the policy via
  the archive cancel-pending seam
  (`cancel_pending_runs=(policy == cancel_pending)`), NOT a
  new worker/wakeup branch (the `lifecycle.py` pause path +
  the archive seam verified shipped).

### 9.1 Slice-2 fork verdict (CLOSED — Q-A=(a) + Q-B; both premises code-verified)

Slice-2 surfaced two genuinely-open points; the CONDUCTOR/
claude-reviewer fork verdict decided both (folded into the
slice-2 commit per the phase-11 §0.3 / phase-12 §0.2 /
phase-13 disposition discipline). Baked verbatim:

- **Q-A = (a) RATIFIED — OneOff OUT of emit-dedup.** §6.4 is
  canonical scope, EmitStep-keyed; OneOff (template-emit, no
  EmitStep) has no §6.4 key. Sentinel (b) = an unrequested
  key-model invention beyond §6.4, forbidden without design
  ratification, a DEFERRED separate question, NOT shipped.
  **A.1:** §0.1/§1/§9 + the §8 tag-annotation + the closeout
  §6.4 design reconciliation scope it explicitly — dedup is
  EmitStep-keyed (§6.4); OneOff OUT by construction (no
  EmitStep, no §6.4 key); its retry/recovery at-most-once is
  the recovery/claim path concern, NOT step-14; the OneOff
  sentinel key is a deferred separate design Q, NOT shipped.
  Honest-scope (phase-9–13 over-claim lesson): NO §1/§7/§8
  wording may imply ALL emits are deduped — only
  source/EmitStep emits. **A.2:** a OneOff fire computes NO
  key, does NOT consult `prior_emit_succeeded`, writes NO
  keyed marker — the `emit_marker_event=None` path,
  behaviour-identical to pre-phase-14 (regression-pinned by
  the unmodified+green phase-9 OneOff suite + the slice-2
  structural A.2 pin).
- **Q-B = mechanism APPROVED — `_commit_success_atomic`
  mirror + universal-terminal-success reroute.** B.1
  structural mirror of `_commit_failure_atomic` (ONE
  `transaction(conn)` = optional `emit_marker_event` + the
  running→succeeded `runs` UPDATE + the `RUN_SUCCEEDED`
  event; both-or-neither; the round-3-hardened
  `rowcount == 0` → rollback guard carried VERBATIM; any
  raise rolls back EVERY write). B.2 `emit_marker_event=None`
  BYTE-BEHAVIOUR-IDENTICAL (OneOff / `succeeded_skipped` /
  no-source / empty-body: same single `RUN_SUCCEEDED` incl.
  the unchanged phase-11 `RunSucceededPayload.skipped_unchanged`
  discriminator, one `assert_legal_transition(RUNNING,
  SUCCEEDED)` at the same caller placement, one transaction,
  same rowcount guard; phase-9 OneOff + phase-11
  succeeded_skipped/skip_unchanged + slice-6
  `RunSucceededPayload` pins UNMODIFIED + green). B.3 three
  success sub-states distinct + pinned (real source delivery
  → `RUN_SUCCEEDED` + keyed `emit_succeeded`; dedup-skip →
  `RUN_SUCCEEDED` + `emit_skipped_idempotent`, NO adapter
  call; phase-11 skip_unchanged → `RUN_SUCCEEDED` +
  `RunSucceededPayload.skipped_unchanged=True`, NO keyed
  marker; OneOff → `RUN_SUCCEEDED` only). B.4 storage
  byte-untouched (`_commit_success_atomic` open-codes
  `transaction(conn)`+`append_event`; does NOT modify
  `update_run_status_and_append_event` — verified empty-diff;
  emit adapters byte-untouched). B.5 NO new EventKind / no
  v002. B.6 the keyed `emit_succeeded` write is in the SAME
  `_commit_success_atomic` transaction as `RUN_SUCCEEDED`,
  durable iff the run terminal-commits; the
  deliver-then-crash-before-this-commit window = the inherent
  at-least-once boundary (pinned; NO exactly-once /
  no-double-deliver claim — R2 honest-window discipline).
  CONTRACTS_V2_DESIGN.md §6.4/§7 amendment stays for the ONE
  closeout reconciliation pass, NOT slice 2.

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
