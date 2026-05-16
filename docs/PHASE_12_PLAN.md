# Phase 12 plan — §12 step 12: read-only-reasoning / emit-only-writes enforcement

Status: **DRAFT for claude-reviewer plan-review round 1.** Not
pushed. Same loop contract as phases 5–11: plan-review rounds →
per-slice implement + review → closeout (full v2 suite +
acceptance walk + phase guard + gen_docs + annotated tag
`v2-phase-12-complete`, tag push gated on a claude-reviewer
CLOSEOUT PASS).

`docs/CONTRACTS_V2_DESIGN.md` §12 step 12 is the canonical
scope source:

> 12. **Read-only reasoning + emit-only writes enforcement**:
> tool-metadata-driven runtime block.

Dependency (design §12): *step 12 depends on 2* (the tool
metadata tag surface). That surface shipped in phase 2
(`app/v2/tool_tags.py`) and was extended in phase 7 (slice 6 —
`FILESYSTEM_READ` / `DB_WRITE`). Phase 12 wires the EXISTING
pure policy into the actual enforcement points.

Design references: §5.4 (tool metadata tags), §5.5 (single
validation entry point), §5.9 (CustomFlow friction triggers),
D6 (read-only reasoning by default).

---

## 0. Scope refinement + open design questions (round-1 input)

### 0.0 What already exists (do NOT rebuild)

- `app/v2/tool_tags.py` — `ToolCapabilityTag` (9 tags) + the
  PURE policy helpers: `is_blocked_by_read_only_reasoning`,
  `requires_admin_approval`, `is_costly`, `requires_oauth`,
  `is_user_facing`. Module docstring explicitly: *"runtime
  composition lands in a later phase."* **That phase is 12.**
- `ToolMode` enum (`app/v2/enums.py:320` —
  `READ_ONLY` / `WRITE_ALLOWED`).
- `ReasoningStep` (`app/v2/models/execution_plan.py:135`) —
  `tool_mode: ToolMode = ToolMode.READ_ONLY`,
  `tools: list[str]`.
- `validate_schedule_spec` (`app/v2/validation.py:477`) — THE
  §5.5 chokepoint; composable independent `_validate_*` rules,
  all-issues-collected contract; `RegistrySnapshot` +
  `_validate_referenced_adapters` already walk plan bodies
  with a registry handle.
- Tools / loaders / authoring tools already carry inline tag
  sets (`tags={ToolCapabilityTag...}`).
- Worker reserves the boundary: a reasoning-bearing plan →
  `_fail_run(reason="reasoning_unsupported_pending_step_12")`
  (phase-11 Q4; `worker.py:687`).

**Missing (= phase-12 deliverable): the enforcement WIRING** —
the pure policy is not yet consulted anywhere that gates a
real authoring / fire path.

### 0.1 Open design questions for round 1

Each carries a RECOMMENDED disposition (reviewer ratifies or
overrides — the phase-11 §0 pattern). Nothing below is
unilaterally decided.

- **Q1 — "runtime block" with no reasoning executor (the
  central scope question).** Design step 12 is a *worker-layer
  runtime block*. But there is no reasoning-chain executor
  (phase-11 §1: *"no §12 step owns it cleanly — step 12 =
  read-only enforcement, presupposes the executor"*); the
  worker `_fail_run`s every reasoning-bearing plan before any
  reasoning runs. A live worker block therefore has nothing to
  fire against yet. Options:
  - **(a) static enforcement + build-the-layer runtime guard
    — RECOMMENDED.** Ship (i) AUTHORING/validation/dry-run/
    freeze/boot-scan static enforcement via the §5.5
    chokepoint, and (ii) the runtime enforcement guard as a
    pure, fully-tested layer + the worker SEAM, NOT exercised
    end-to-end (no executor) — mirrors the phase-10
    build-the-layer discipline. The worker keeps `_fail_run`ing
    reasoning-bearing plans (phase-11 boundary unchanged).
  - (b) static-only — no runtime guard layer until the
    executor lands.
  - (c) build-the-layer runtime guard only — skip the static
    authoring enforcement.
  - *Rationale for (a):* maximises shippable, testable value
    without the executor; reuses the reviewer-approved
    build-the-layer pattern; zero regression to the phase-11
    worker boundary.
- **Q2 — tool→tag resolution source of truth.**
  `ReasoningStep.tools` is `list[str]`. Resolution to a tag
  set is needed. RECOMMENDED: reuse the validation
  `RegistrySnapshot` registry seam (same path
  `_validate_referenced_adapters` already uses). Fail-safe per
  §5.4: an unregistered / untagged referenced tool is treated
  as `write_external` (⇒ blocked under `read_only`) and emits
  a distinct `ValidationIssue` code, never silently allowed.
- **Q3 — worker reasoning boundary interaction.** RECOMMENDED:
  phase 12 does NOT build the executor and does NOT un-reserve
  the boundary — the worker still cleanly `_fail_run`s
  reasoning-bearing plans. The reason-code *message* may be
  refined to state the step-12 enforcement layer now exists
  but the executor is still pending; the `_fail_run` BEHAVIOUR
  is byte-unchanged (phase-11 acceptance pins must stay green).
- **Q4 — emit-step adapter tags are NOT enforced against.**
  Emit adapters are `write_external` / `send_message` by
  design — they are the sanctioned write path. RECOMMENDED:
  enforcement is scoped STRICTLY to `ReasoningStep.tool_mode`
  vs the step's referenced tool tags (the D6 concern).
  Enforcing tags on emit steps would contradict the design;
  state this explicitly so the boundary is unambiguous.
- **Q5 — §5.9 CustomFlow friction scope.** §5.9 lists seven
  triggers; only three are tag-driven (the
  `requires_admin_approval` set: `privileged` / `costly` /
  `filesystem_write`, plus a `tool_mode=write_allowed`
  reasoning step). RECOMMENDED: phase 12 wires ONLY the
  tag-driven subset; the orthogonal triggers (dynamic delivery
  target, emit count > 3, previously-unused adapter,
  `require_reapprove` + recent shape change) are out of scope
  for step 12 (a later authoring-friction step).
- **Q6 — single-chokepoint placement.** RECOMMENDED: the new
  static rule rides `validate_schedule_spec` as one more
  independent `_validate_*` (so it is enforced at every §5.5
  funnel — ADK tool layer, template factories, CustomFlow,
  `schedule_freeze`, worker boot scan — for free), NOT a
  separate freeze-only check. All-issues-collected contract
  preserved (the rule is additive and independent).

---

## 1. Scope statement

### In scope (phase 12), assuming the Q1(a) / Q2–Q6
RECOMMENDED dispositions (reviewer may revise):

1. **Pure enforcement composition.** A pure decision layer
   (extend `tool_tags.py` or a new sibling module) that, given
   a `ReasoningStep` (its `tool_mode` + resolved tool tag
   sets), returns a typed allow/block outcome with the
   offending tool(s) + tag(s). No I/O. Built ON the existing
   `is_blocked_by_read_only_reasoning` helper, not a reimpl.
2. **Static authoring enforcement.** A new
   `validate_schedule_spec` rule walking every
   `ExecutionPlan.reasoning` step: a `tool_mode=read_only`
   step that references a read-only-blocking-tagged tool is a
   hard validation error; unresolved/untagged tool → fail-safe
   block (Q2). New `ValidationIssue` codes; additive,
   independent, all-issues-collected preserved. Enforced at
   every §5.5 chokepoint via the single entry point (Q6).
3. **Tag-driven CustomFlow friction (subset, Q5).** A
   `tool_mode=write_allowed` reasoning step, or any referenced
   tool tagged `privileged` / `costly` / `filesystem_write`,
   trips the EXISTING admin-approval / handshake friction path
   — additive to the phase-7/8 authoring flow.
4. **Build-the-layer runtime guard + worker seam (Q1a).** The
   runtime enforcement function + the worker reasoning seam,
   pure and unit-tested, NOT fired end-to-end (no executor).
   The worker continues to `_fail_run` reasoning-bearing
   plans (Q3 — boundary behaviour unchanged); a seam test
   pins the guard is reachable-but-deferred.
5. **Hygiene + docs.** AST import-hygiene pin for any new
   module (no `datetime.now`/`uuid4`/vendor-SDK module-load —
   alias-robust, the phase-11 slice-8 detector). The
   `tests/v2/test_tool_tags.py` drift-guard extended to cover
   the new composition. `PHASE_ALLOWLIST[12]` +
   `.v2-current-phase`→12 land in the plan commit (this
   commit). `gen_docs.py` regen + stage at closeout.

### Out of scope (phase 12) — deferred with rationale

- **The LLM `ReasoningStep` chain executor.** No §12 step
  owns it cleanly (phase-11 §1). The worker keeps the
  phase-11 `_fail_run` boundary.
- **Non-tag §5.9 friction triggers** (dynamic target, emit
  count > 3, unused adapter, `require_reapprove` + shape
  change) — a later authoring-friction step.
- **Emit-adapter tag enforcement** — emit adapters are the
  sanctioned write path (Q4).
- **Cross-fire state, idempotency, observability, migration
  tooling** — §12 steps 13–16.

---

## 2. New file paths (provisional — finalised per slice)

- `app/v2/reasoning_enforcement.py` (or an additive extension
  of `app/v2/tool_tags.py` — reviewer's call in round 1) — the
  pure composition + the runtime guard function.
- `tests/v2/test_reasoning_enforcement.py` — pure-policy +
  guard unit tests.
- `tests/v2/test_phase12_import_hygiene.py` — AST pin (mirror
  phase-11 slice-8, alias-robust).
- Extensions only (no new file): `app/v2/validation.py` (new
  `_validate_reasoning_tool_mode` rule + codes), the authoring
  friction path (slice 3), `app/v2/runtime/worker.py` (the
  seam, slice 4).

## 3. Module APIs (sketch — finalised per slice)

- `evaluate_reasoning_step(step, *, resolve_tags) ->
  ReasoningEnforcementOutcome` — pure; `resolve_tags:
  Callable[[str], AbstractSet[ToolCapabilityTag] | None]`;
  outcome carries `allowed: bool`, `blocked_tools:
  list[tuple[str, frozenset[ToolCapabilityTag]]]`,
  `unresolved_tools: list[str]`. `write_allowed` mode ⇒
  allowed (friction is the authoring layer's concern, Q5).
- `_validate_reasoning_tool_mode(spec, execution_plans,
  registries) -> list[ValidationIssue]` — additive
  `validate_schedule_spec` rule (Q6).
- Worker seam (slice 4): a guard call positioned where the
  reasoning executor WOULD consult it, behind the unchanged
  phase-11 `_fail_run` boundary (Q1a / Q3).

## 4. Slice ordering + commit cadence (draft — reviewer
finalises)

Slice-gated; pause after each commit for reviewer; no push
mid-phase.

0. **plan + phase transition** (this commit; NOT pushed) —
   `docs/PHASE_12_PLAN.md`, `PHASE_ALLOWLIST[12]`,
   `.v2-current-phase`→12. Design doc NOT touched here (the
   ONE reconciliation pass is at closeout).
1. **Pure enforcement composition** + unit tests +
   `test_tool_tags` drift-guard extension + AST hygiene pin.
2. **`validate_schedule_spec` static rule** (Q2/Q6) — new
   `_validate_reasoning_tool_mode` + codes; coupled-test
   reconcile; all-issues-collected proof.
3. **Tag-driven CustomFlow friction** (Q5 subset) — additive
   to the authoring path.
4. **Build-the-layer runtime guard + worker seam** (Q1a/Q3)
   — guard wired at the seam, NOT live; phase-11 worker
   boundary regression-pinned unchanged.
5. **closeout** — full `tests/v2`, §7 acceptance walk, phase
   guards, `gen_docs` regen+stage, REPO-WIDE semantic-intent
   stale-wording sweep (the phase-9/10/11 lesson), the ONE
   `docs/CONTRACTS_V2_DESIGN.md` reconciliation pass (§12 step
   12 scope/shipped, §5.4 / §5.9 / D6 LIVE notes), annotated
   tag `v2-phase-12-complete` (gated on reviewer CLOSEOUT
   PASS).

## 5. Test inventory (highlights)

- `test_reasoning_enforcement.py` — read_only blocks each
  blocking tag; `write_allowed` allows; multi-tag composition;
  unresolved-tool fail-safe block; `filesystem_read` /
  `read_external` NOT blocking.
- `test_validation*` (extend) — a read_only step referencing a
  write-tagged tool fails `validate_schedule_spec`; all-issues
  collected (rule independent); emit-only path unaffected.
- `test_authoring_*` (extend) — `write_allowed` step /
  privileged-costly-filesystem_write tool trips friction.
- `test_runtime_worker_emit_branch` (extend) — the phase-11
  `reasoning_unsupported_pending_step_12` `_fail_run` is
  byte-unchanged (Q3 regression pin); the build-the-layer
  guard is reachable-but-deferred.
- `test_phase12_import_hygiene.py` — alias-robust no
  `uuid.uuid4` / `datetime.now` / vendor-SDK module-load.

## 6. CI guard checks

- `check_phase_scope.py --staged` and
  `--diff v2-phase-11-complete` exit 0; `PHASE_ALLOWLIST[12]`
  enumerates exactly the phase-12 surface (no agent mount —
  mirrors phase-10 build-the-layer).
- AST import-hygiene pin (alias-robust, phase-11 slice-8
  carry-forward).
- `phase >= 9` doc-coupling (behaviour change paired with the
  matching hand-written doc; `ORI_SKIP_DOC_CHECK` is
  harness-DENIED — never bypass).
- `gen_docs.py` regen at closeout; stage `docs/INDEX.md` /
  `docs/AGENTS_INVENTORY.md` only if `files_changed != 0`.
- Full `tests/v2` green, no regression (2286 baseline +
  phase-12 adds).

## 7. Acceptance criteria for `v2-phase-12-complete`
(provisional — finalised after round-1 disposition)

1. Branch ahead of `v2-phase-11-complete` by N small per-slice
   commits.
2. A `tool_mode=read_only` reasoning step referencing a
   `write_external` / `send_message` / `filesystem_write` /
   `db_write` / `privileged` tool is REJECTED at
   `validate_schedule_spec` (⇒ every §5.5 chokepoint);
   `filesystem_read` / `read_external` do NOT block.
3. An unresolved / untagged referenced tool fails safe
   (blocked) with a distinct `ValidationIssue` code — never
   silently allowed (§5.4 fail-safe).
4. `tool_mode=write_allowed` step / `privileged` / `costly` /
   `filesystem_write` tool trips the existing admin-approval
   friction (Q5 subset).
5. The pure composition reuses `tool_tags.py` helpers (no
   reimplementation); `test_tool_tags` drift-guard extended
   and green.
6. The phase-11 worker reasoning boundary
   (`reasoning_unsupported_pending_step_12` `_fail_run`) is
   behaviourally byte-unchanged (regression-pinned). No
   reasoning executor shipped.
7. Emit adapters are NOT tag-enforced (Q4); OneOff / v1 /
   phase-9–11 source path byte/behaviour-unchanged (§11.1
   additive).
8. No `datetime.now` / `uuid.uuid4` / vendor-SDK module-load
   in any phase-12 NEW module; alias-robust AST pin green.
9. Phase guard `--staged` + `--diff v2-phase-11-complete`
   exit 0.
10. Full `tests/v2` green; no regression; §7 walked with
    evidence.
11. plan ≡ shipped code ≡ design ≡ tag annotation, zero
    divergence (one reconciliation pass at closeout;
    semantic-intent sweep, not literal-token).
12. Annotated tag `v2-phase-12-complete` created (push gated
    on reviewer CLOSEOUT PASS, phases 5–11 pattern).

## 8. Tag annotation (draft — finalised at closeout)

```
v2 phase 12 complete

Read-only-reasoning / emit-only-writes enforcement (§12
step 12). The pure tool-capability-tag policy
(app/v2/tool_tags.py, phase 2/7) is wired into the
enforcement points: validate_schedule_spec rejects a
tool_mode=read_only reasoning step that references a
read-only-blocking-tagged tool (write_external /
send_message / filesystem_write / db_write / privileged),
enforced at every §5.5 chokepoint; unresolved/untagged
referenced tools fail safe (blocked). tool_mode=write_allowed
steps and privileged/costly/filesystem_write tools trip the
existing admin-approval friction (the tag-driven §5.9
subset). The runtime guard + worker seam ship build-the-layer
(pure, tested) but are NOT fired end-to-end: there is no LLM
reasoning-chain executor (no §12 step owns it), so the
phase-11 worker boundary
(_fail_run reason=reasoning_unsupported_pending_step_12) is
behaviourally unchanged. Emit adapters are the sanctioned
write path and are NOT tag-enforced. OneOff / v1 / the
phase-9–11 source fire path are byte/behaviour-unchanged
(§11.1 additive).

NOT shipped (deferred): the reasoning-chain executor; the
non-tag §5.9 friction triggers; cross-fire state (step 13);
idempotency/cancellation (step 14); observability (step 15);
migration tooling (step 16).

Design: docs/CONTRACTS_V2_DESIGN.md §12 step 12, §5.4, §5.5,
        §5.9, D6
Plan:   docs/PHASE_12_PLAN.md
```

## 9. claude-reviewer round-1 disposition (OPEN)

Q1–Q6 above await round-1 adjudication. Decisions will be
baked here verbatim (the phase-10/11 disposition-log
discipline) so a future drift is caught against the decision,
not re-litigated.

## 10. Hard rules (carried forward from phases 9–11)

- Slice-gated; pause after each commit for claude-reviewer;
  no push without a CLOSEOUT PASS.
- Invariants phase 12 must NOT regress: §3.5 typed-error
  taxonomy + `fallback_eligible`; resolver
  exactly-one-terminal-outcome / never-raise (UNTOUCHED —
  phase 12 does not touch sources/resolver); cache `ttl>0`
  no-probe / `==0` probe-every-fire (UNTOUCHED); §5.3.1
  dirfd fence (UNTOUCHED); Q5 worker pure snapshot consumer
  (UNTOUCHED); §11.1 additive cutover (OneOff / v1 /
  phase-9–11 byte/behaviour-unchanged); the phase-11 step-12
  worker boundary (`reasoning_unsupported_pending_step_12`
  `_fail_run`) NOT regressed; `tool_tags.py` pure-contract
  (no I/O) preserved; `validate_schedule_spec`
  all-issues-collected contract preserved (the new rule is
  additive + independent).
- `uv run python …` always; no `sed`; refresh
  `.docs_read_marker` via
  `echo "yes" | uv run python scripts/check_docs_read.py`
  before each commit; commit trailer
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- Leave `app/tools/youtube.py` dirty + `.playwright-mcp/` +
  `scripts/{amazon_ads_mcp_proxy,diag_gemini_caching,diag_removal_order}.py`
  untracked — never stage.
- plan ≡ design ≡ code ≡ tag, semantic-intent reconciliation
  (the phase-9/10/11 stale-wording lesson — not just the
  literal-token set); design doc amended only in the ONE
  closeout reconciliation pass.
