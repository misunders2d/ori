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
  Reviewer rules the cut.
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
   + runs + schedules read substrate (Q3 set). Typed result
   models. NO SQL re-implementation — compose the shipped
   `storage/*` pure-read API. ZERO fire-path touch.
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
2. **Failure-monitor pure detector** — build-the-layer
   function (DI-conn / DI-clock), NOT wired; reuses
   `admin_alert_sent` / `admin_alert_acked` (no new kind).
   Carries the §11.1 byte-proof + phase-9–14 boundary
   regression pins (the live-substrate slice).
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

Observability (§12 step 15). Pure read-only query /
aggregation primitives over the live EventLedger substrate
(written every fire by the phase-9–14 cores) — composes the
shipped storage/* pure-read API, NO SQL re-implementation,
ZERO fire-path behaviour change (pure side-channel). The
background failure monitor ships as a build-the-layer pure
detector (DI-conn / DI-clock) — NO periodic wiring, NO
re-alert side-effect, reuses admin_alert_sent /
admin_alert_acked. No new EventKind, no v002.

Deferred: schedule_replay (touches the dry-run/fire path);
the failure-monitor periodic wiring + re-alert dispatch;
external metrics/trace exporter (not canonical §9);
migration tooling (§12 step 16); the reasoning/stateful-flow
executor + the retry CHAIN (no §12 step owns either).

Phase-9–14 fire path + emit adapters byte/behaviour-
unchanged; carried invariants intact.

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 15, §9
Plan:   docs/PHASE_15_PLAN.md
```

## 9. claude-reviewer round-1 disposition (PENDING)

Round 1 to be baked here VERBATIM (the phase-10–14
disposition-log discipline) so a future drift is caught
against the decision, not re-litigated. Any code-busted
premise gets a verbatim record + an inline **SUPERSEDED**
annotation (the phase-14 §0.1/§9.2 precedent — a busted
premise must NOT silently persist as plan wording; the
phase-9–14 stale-wording lesson).

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
