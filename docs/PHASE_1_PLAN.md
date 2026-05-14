# v2 Phase 1 — implementation plan (draft)

> **PLAN ONLY — NO CODE.** Awaiting review before implementation
> begins. Companion to the approved design contract at
> `docs/CONTRACTS_V2_DESIGN.md`. Safety tag
> `pre-contracts-v2-2026-05-14` is the revert point.

---

## 1. Scope statement

### In scope (phase 1)

Per §12 step 1 + §12.1 of the design contract:

- Pydantic models for ScheduleSpec, ExecutionPlan (+ its nested
  shapes), Run, Event, ScheduleState, SourceSnapshotMetadata, and
  the Trigger union.
- All canonical enums (RunStatus, FireReason, EventKind,
  ScheduleStatus, EnforcementMode, plus the FailureActionType /
  ScheduleStatus / etc. already used by v1).
- SQLite DDL for the 7 phase-1 tables (`schedules`,
  `execution_plans`, `runs`, `events`, `schedule_state`,
  `source_snapshots`, `applied_migrations`).
- Migration runner skeleton — apply_pending() + applied_migrations
  tracking. Phase 1 ships ONE migration: `v001_initial`.
- Unit tests for: model validation, state-machine transitions
  (§4.0.1 table), invariants (§4.0.4), migration apply +
  idempotency.
- CI guard scripts that enforce phase scope.
- Phase-completion tag.

### Out of scope (phase 1)

- Worker pool execution.
- APScheduler wakeup callback wiring.
- Agent-facing ADK tools.
- Adapter / source loader registration.
- Registry cache (Slack/Drive enums).
- Dry-run handshake.
- Boot self-test.
- Any change to v1 code paths (`app/contracts/`, `app/tasks.py`,
  `app/contracts/executor.py`, `app/scheduler_instance.py`,
  `data/contracts/`).
- Any change to Coordinator instruction or agent skill files.
- Any change to existing CI workflows beyond adding the new phase
  guard.

If phase 1 PR touches anything in the out-of-scope list, the CI
guard (§6 below) rejects the merge.

---

## 2. New file paths

```
app/v2/
    __init__.py
    enums.py
    models/
        __init__.py
        schedule.py
        execution_plan.py
        run.py
        event.py
        state.py
        snapshot.py
        triggers.py
        common.py
    ddl/
        __init__.py
        v001_initial.sql
    migrations/
        __init__.py
        runner.py
        v001_initial.py

tests/v2/
    __init__.py
    conftest.py
    test_models_schedule.py
    test_models_execution_plan.py
    test_models_run.py
    test_models_event.py
    test_models_state.py
    test_models_snapshot.py
    test_models_triggers.py
    test_lifecycle_transitions.py
    test_invariants.py
    test_idempotency_key.py
    test_root_run_id.py
    test_migrations.py
    test_ddl_constraints.py
    test_ci_guards.py

scripts/
    check_phase_scope.py                        # CI-guard logic (shared between local + GH Action)
    install_hooks.py                            # EXISTING file; phase 1 appends one line to wire the new pre-commit guard

.githooks/
    v2_phase_guard.sh                           # pre-commit invocation of check_phase_scope.py

.github/workflows/
    v2_phase_guard.yml                          # CI workflow invoking check_phase_scope.py on PRs

.v2-current-phase                                # repo root; single integer
```

**Notable absences (intentional):**

- No `app/v2/worker.py`, no `app/v2/executor.py`, no
  `app/v2/wakeup.py`. Phase 1 has no runtime code.
- No `app/v2/storage/` CRUD helpers. Phase 3 introduces those.
- No `app/v2/registry/`. Phase 5 introduces the cache.
- No `app/v2/adapters/`, no `app/v2/sources/`. Phases 7 and 9.

---

## 3. Migration strategy

Forward-only, append-only migrations. Tracking via dedicated
table.

### 3.1 Migration runner contract

```
# app/v2/migrations/__init__.py
class Migration(Protocol):
    id: str                                     # e.g. "v001_initial"
    description: str
    def apply(self, conn: sqlite3.Connection) -> None: ...

# app/v2/migrations/runner.py
MIGRATIONS: list[Migration] = [V001Initial()]

def apply_pending(conn: sqlite3.Connection) -> list[str]:
    """Apply every Migration in MIGRATIONS not already in
    applied_migrations. Returns list of newly-applied ids.
    Idempotent: re-runs are no-ops.

    Pre-migration setup:
      1. Set PRAGMA journal_mode=WAL OUTSIDE any transaction
         (SQLite forbids journal-mode change inside a TX).
         Skipped if the DB is already in WAL mode.
      2. Set PRAGMA foreign_keys=ON for this connection.
      3. Ensure the applied_migrations bookkeeping table exists
         (the runner owns this — individual migrations never
         touch it; see §3.6).

    Per-migration:
      BEGIN; <migration.apply(conn)>; INSERT into applied_migrations; COMMIT.
      Failure rolls back the migration's tables + the insert.

    NEVER runs against a production DB implicitly. Caller must
    pass an explicit `conn` (see §3.3 decision).
    """
    ...

def applied_ids(conn: sqlite3.Connection) -> set[str]:
    """Read applied_migrations table. Returns empty set if the
    table doesn't exist yet."""
    ...
```

### 3.2 v001_initial migration

Single migration. Contents (in DDL form per §4.0.2 of the design
contract):

- `CREATE TABLE schedules (...)` per §4.0.2
- `CREATE TABLE execution_plans (...)`
- `CREATE TABLE runs (...)` — with `root_run_id NOT NULL` and
  retry-chain indexes per the round-6 update.
- `CREATE TABLE events (...)`
- `CREATE TABLE schedule_state (...)`
- `CREATE TABLE source_snapshots (...)`
- All `CREATE INDEX` statements from §4.0.2.

The whole migration runs in a single `BEGIN ... COMMIT`
transaction. Partial apply impossible.

**NOT inside the v001 transaction** (per review round 7
correction): `PRAGMA journal_mode=WAL` cannot run inside a
transaction — SQLite forbids it. The runner handles WAL setup
BEFORE opening the migration TX (see §3.1 pre-migration setup).
v001 owns only the v2 CREATE TABLE / CREATE INDEX statements.

**NOT inside the v001 SQL either**: the `applied_migrations`
bookkeeping table is owned by the runner (§3.6). v001 never
references it.

### 3.3 Database file (DECISION, not recommendation)

Phase 1 migrations RUN ONLY against test DBs. `apply_pending(conn)`
takes an explicit connection — no implicit `data/ori-scheduler.db`
access in phase 1.

Production DB sees v2 tables only when phase 4 (wakeup callback)
lands; that phase introduces an explicit setup step that calls
`apply_pending(production_conn)` once at boot. Until then, the
production DB has zero v2 schema.

Test code constructs ephemeral SQLite databases under `tmp_path`
(pytest fixture in §5.5).

### 3.4 Migration file naming

`v<NNN>_<snake_case_label>.py` + matching `.sql`. NNN is
zero-padded 3 digits. Forward-only ordering by id. Never edit a
migration after it has shipped to any environment; new changes
are NEW migration files.

### 3.5 Single bookkeeping table, runner-owned

The `applied_migrations` table is created by the runner BEFORE
running any user migration. Individual migrations (`v001_initial`
and successors) never touch this table — they only own their own
business tables / indexes.

Bootstrap logic (executed by `apply_pending` on EVERY call):

```sql
CREATE TABLE IF NOT EXISTS applied_migrations (
    id          TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
```

This statement runs outside the per-migration transaction. It's
idempotent (`IF NOT EXISTS`) and cheap; no harm in re-running it
on every `apply_pending` call.

After each migration's `apply(conn)` succeeds inside its
transaction, the runner does ONE additional statement in the
same TX:

```sql
INSERT INTO applied_migrations (id, applied_at) VALUES (?, ?);
```

Then `COMMIT`. If the migration body or the insert fails, both
roll back. The migration's tables remain absent on the next
attempt; the bookkeeping table stays empty for that id.

---

## 4. Pydantic model inventory

Pydantic v2 conventions, `model_config = ConfigDict(extra="forbid")` on every model.

### 4.1 `app/v2/enums.py`

```
ScheduleStatus      ∈ {active, paused, archived}
RunStatus           ∈ {pending, claimed, running, succeeded, failed, cancelled}
FireReason          ∈ {scheduled, manual, retry, replay, backfill}
EnforcementMode     ∈ {strict, permissive}
EventKind           ∈ <full list from §4.0 of design contract>
SourceMode          ∈ {literal, snapshot, live}            # version_pinned was collapsed into LiveSourceRef(version=...) per design §4.6
SelectionMethod     ∈ {stable_id, content_hash, row_number}
RecoveryPolicy      ∈ {queue_retry, mark_failed, clear_claim}
OnOversizePolicy    ∈ {fail_and_alert, store_pointer_only, redact_and_store, hash_only_no_replay}
LiveChangePolicy    ∈ {allow, alert_on_shape_change, require_reapprove_on_shape_change}
SourceFallbackPolicy ∈ {use_last_good_snapshot, alert_and_skip, alert_and_use_default}
FailureActionType   ∈ {alert_admin, retry_later, abort_silent, custom}
ToolMode            ∈ {read_only, write_allowed}
```

### 4.2 `app/v2/models/triggers.py`

```
CronTrigger(type: Literal["cron"], cron: str, timezone: str)
OneOffTrigger(type: Literal["one_off"], at_iso_datetime: datetime, timezone: str)
IntervalTrigger(type: Literal["interval"], every_seconds: int)  # phase-3 stub
EventTrigger(type: Literal["event"], event: str)                # phase-3 stub
ConditionalTrigger(type: Literal["conditional"], gate: str, poll_seconds: int)  # phase-3 stub

Trigger = Annotated[
    Union[CronTrigger, OneOffTrigger, IntervalTrigger, EventTrigger, ConditionalTrigger],
    Field(discriminator="type"),
]
```

Phase 1 ships all five trigger classes but only `cron` and
`one_off` have non-trivial validators. The other three are
declared so the discriminator union is complete; their wakeup /
gate evaluation is a phase-3 concern.

### 4.3 `app/v2/models/common.py`

```
UserRef(platform: str, user_id: str, display_name: Optional[str])
ChannelRef(kind: Literal["slack", "telegram"], external_id: str)
SheetRef(spreadsheet_id: str, range_: str)
TemplateRef(name: str, version: str)

Delivery(
    target_session_id: str,
    fallback_policy: Literal["session_to_origin", "admin_alert_only"],
)

FailurePolicy(
    on_failure_action: FailureActionType,
    retry_policy: Optional[RetryPolicy]   # backoff, max_retries
)

RetryPolicy(strategy: Literal["exponential", "fixed"], base_seconds: int, max_attempts: int)

AuditPolicy(
    keep_last_n_snapshots: int,
    dedup_by_content_hash: bool,
    redact_fields: list[str],
    max_snapshot_bytes: int,
    on_oversize: OnOversizePolicy,
)
```

### 4.4 `app/v2/models/schedule.py`

```
ScheduleSpec(
    id: str                                     # pattern ^[a-z][a-z0-9_]*$
    owner: UserRef
    description: str                            # ≥ 8 chars
    trigger: Trigger
    delivery: Delivery
    failure: FailurePolicy
    audit: AuditPolicy
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    execution_plan_hash: Optional[str]
    template: Optional[TemplateRef]
    authored_at: datetime
    parent_hash: Optional[str]
    hash: str                                   # SHA-256 over canonical body
)
```

Pre-existing v1 hash-computation helpers will be ported, NOT
reused — `app/contracts/schema.py` stays untouched. The new
`compute_hash()` lives on `ScheduleSpec` itself.

### 4.5 `app/v2/models/execution_plan.py`

```
InputSpec(id, loader, args, cache_for_seconds)
OutputSpec(type, schema_, constraints)
Retry(on_validation_fail, on_tool_error)

ReasoningStep(
    id, description, entry_agent, transfers_allowed,
    tools, tool_mode: ToolMode = ToolMode.READ_ONLY,
    model, user_template, output, retry, max_tool_calls
)

Gate(type, args)

EmitStep(
    id: str                                     # REQUIRED — see §4.13/§6.4 of design contract
    adapter: str
    args: dict
    gate: Optional[Gate]
    abort_on_gate_fail: bool = False
)

Acceptance(checks)
FailureAction(action, notify, retry_after_minutes, abort)

ExecutionPlan(
    id, version, hash, description, author, authored_at,
    parent_hash, inputs, reasoning, emit, acceptance,
    on_failure, enforcement
)
```

Phase 1 ships these models WITHOUT any validators that depend on
runtime registries (registry/loader/adapter validators are
phase-2). Only Pydantic shape validation here.

### 4.6 `app/v2/models/run.py`

```
Run(
    id: str                                     # uuid4
    schedule_id: str
    execution_plan_hash: Optional[str]
    fire_reason: FireReason
    due_at: datetime
    status: RunStatus
    attempt: int = 1
    root_run_id: str                            # NOT NULL; self-ref for first attempt
    parent_run_id: Optional[str]                # previous attempt
    claimed_by: Optional[str]
    claimed_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    error: Optional[str]
)
```

Validator: `parent_run_id` is null iff `attempt == 1` AND
`root_run_id == id`. Mismatch → reject.

### 4.7 `app/v2/models/event.py`

```
Event(
    id: str                                     # uuid4
    run_id: Optional[str]
    schedule_id: str
    ts: datetime
    kind: EventKind
    payload: dict
    correlates: Optional[str]
)
```

Phase 1 keeps `payload` as a freeform dict. Phase 2 adds per-kind
Pydantic payload models so payloads can be type-checked too.

### 4.8 `app/v2/models/state.py`

```
ScheduleState(
    schedule_id: str
    key: str
    value: Any                                  # JSON-serialisable
    version: int                                # bumped on each write
    written_at: datetime
    written_by_run: Optional[str]
)
```

### 4.9 `app/v2/models/snapshot.py`

```
SourceSnapshotMetadata(
    run_id: str
    source_id: str
    content_hash: str                           # sha256:<hex>
    content_path: str                           # relative to project root
    content_size: int
    fetched_at: datetime
    source_kind: str
    source_version: Optional[str]
    selection_method: SelectionMethod
)
```

---

## 5. Test file inventory (phase 1)

All under `tests/v2/`. Each test file is small and focused.
Pytest, async tests via `pytest-asyncio` where needed (most
phase-1 tests are sync).

### 5.1 Model unit tests

`test_models_schedule.py`
- `ScheduleSpec` id pattern (snake_case, lowercase, must start with letter)
- `status` defaults to `active`
- `execution_plan_hash` is None ⇒ valid lightweight reminder
- `hash` is deterministic over canonical body (re-serialise, recompute, equal)
- `parent_hash` chain validation
- `description` minimum length

`test_models_execution_plan.py`
- `EmitStep.id` is required (regression for the round-6 finding)
- `ReasoningStep.tool_mode` defaults to `read_only`
- `enforcement` defaults to `strict`
- `inputs` empty, `reasoning` empty, but `emit` present → valid (covers reminder case via plan)
- Hash determinism over canonical body
- Cross-ref placeholders in `emit.args` — phase 1 only checks shape, deeper validation is phase 2

`test_models_run.py`
- `root_run_id == id` for first-attempt rows; mismatch rejected
- `attempt = 1` ⇒ `parent_run_id is None`
- `attempt >= 2` ⇒ `parent_run_id is not None`
- `status` constrained to canonical enum

`test_models_event.py`
- All canonical EventKinds round-trip (json → model → json)
- `correlates` is None for primary events; set on `*_acked` events

`test_models_state.py`
- `version` starts at 1
- `value` accepts JSON-serialisable shapes (str, int, list, dict)
- `written_by_run` may be None (for author-time seeds)

`test_models_snapshot.py`
- `content_hash` matches `sha256:<hex>` pattern
- `content_path` is relative (no leading `/`)

`test_models_triggers.py`
- Discriminator union dispatches to correct subclass by `type` field
- `CronTrigger.cron` parseable (only basic check; full validation deferred)
- `OneOffTrigger.at_iso_datetime` accepts ISO 8601 strings

### 5.2 Lifecycle / invariant tests

`test_lifecycle_transitions.py`
- Every transition in §4.0.1's full transition table has a test
  case constructing the source state, calling the transition
  helper, asserting the destination state + event(s) written.
- Helper functions live in test fixtures — the actual transition
  logic is phase-4 worker code; phase 1 tests work against the
  documented contract.

`test_invariants.py`
- One test per phase-1 invariant in §4.0.4:
  - Run claim invariant (cannot transition non-pending → claimed)
  - Retry chain invariant (failed never becomes pending; retry is
    a new row with `root_run_id` propagated)
  - Emit idempotency invariant (key formula stable across
    retries with same root_run_id)
  - Ledger transactionality (a state change + event write
    succeed or fail together)
  - Recovery on boot (stale claimed/running gets routed per
    policy)
  - Backfill invariant (missed due_at creates explicit runs
    within window)
  - Pause invariant (paused schedules create no new runs)
  - Archive invariant (archived schedules cannot create runs)

`test_idempotency_key.py`
- `compute_idempotency_key(schedule_id, root_run_id, emit_id)`
  returns deterministic value
- Different `attempt` produces SAME key when `root_run_id` is
  shared (the round-6 regression)
- Different `run_id` produces SAME key when `root_run_id` is
  shared
- Different `emit_id` produces DIFFERENT key
- Different `root_run_id` produces DIFFERENT key

`test_root_run_id.py`
- First attempt: `root_run_id == id`
- Second attempt (retry): `root_run_id == first attempt's id`
- Third attempt: same `root_run_id` as first two
- Recursive CTE-style retrieval finds all attempts in chain

### 5.3 Migration / DDL tests

`test_migrations.py`
- Fresh DB: `apply_pending()` runs v001, returns `['v001_initial']`
- Re-apply on already-migrated DB: returns `[]`, no rows changed
- Migration is atomic — simulate failure mid-apply (mock conn
  raises after partial DDL); applied_migrations does NOT contain
  v001_initial; tables that DO exist are cleanly removable
- `applied_migrations` table is created before any other CREATE
  TABLE

`test_ddl_constraints.py`
- `runs.status` rejects invalid enum value
- `runs.fire_reason` rejects invalid enum value
- `schedules.status` rejects invalid enum value
- Foreign keys enforced when `PRAGMA foreign_keys = ON`
  (inserting Run with unknown schedule_id raises)
- `runs.root_run_id NOT NULL` constraint holds

### 5.4 CI guard tests

`test_ci_guards.py`
- `check_phase_scope.py` accepts a phase-1 diff that touches only
  `app/v2/` and `tests/v2/`
- Rejects a diff that touches `app/contracts/`
- Rejects a diff that touches `app/tasks.py`
- Rejects a cross-phase diff touching `app/v2/` AND
  `app/sub_agents/coordinator_agent.py` without the explicit
  override marker
- Override marker (`PHASE_OVERRIDE: justification` in commit
  message) lets a cross-phase commit pass with a logged warning

### 5.5 Fixtures (`tests/v2/conftest.py`)

- `tmp_db` fixture: per-test SQLite DB at `tmp_path`, pre-migrated
- `sample_schedule_spec`: builder helper for a valid ScheduleSpec
- `sample_execution_plan`: builder for a minimal ExecutionPlan
- `sample_run`: helper to construct Runs at any status
- `sample_event`: helper for Events of any kind

---

## 6. CI guard checks

### 6.1 Local pre-commit hook

`.githooks/v2_phase_guard.sh`:

```bash
#!/usr/bin/env bash
# Invoked from .githooks/pre-commit AFTER the existing doc-read
# check. Runs ``scripts/check_phase_scope.py --staged`` and
# blocks the commit on non-zero exit.
exec uv run python scripts/check_phase_scope.py --staged
```

Hook is opt-in via `scripts/install_hooks.py` (which already
exists for the v1 doc-read marker). Phase-1 PR adds a line to
that installer.

### 6.2 GitHub Actions workflow

`.github/workflows/v2_phase_guard.yml`:

- Triggers: every PR
- Runs `scripts/check_phase_scope.py --diff origin/main...HEAD`
- Hard fail on cross-phase diff without override marker
- Posts a comment on the PR explaining which files violated

### 6.3 `scripts/check_phase_scope.py` logic

```
PHASE_ALLOWLIST: dict[int, set[str]] = {
    1: {
        "app/v2/",
        "tests/v2/",
        "scripts/check_phase_scope.py",
        "scripts/install_hooks.py",            # phase 1 appends a line wiring the new hook
        ".githooks/v2_phase_guard.sh",
        ".github/workflows/v2_phase_guard.yml",
        ".v2-current-phase",
        "docs/PHASE_1_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
    },
    2: {"app/v2/", "tests/v2/"},
    # ... per phase as it lands
}

FORBIDDEN_PHASES_1_TO_7: set[str] = {
    "app/contracts/", "app/tasks.py", "app/scheduler_instance.py",
    "data/contracts/", "app/contracts/executor.py",
}

def current_phase() -> int:
    """Read .v2-current-phase file (single integer)."""
    ...

def check(diff_files: list[str]) -> list[str]:
    """Return list of violations; empty list = OK."""
    ...
```

### 6.4 Override marker — requires human ack

A commit can opt out of the guard by including
`PHASE_OVERRIDE: <justification>` in the commit message body.
The script logs the override and lets the local pre-commit hook
pass, BUT the GitHub Actions workflow marks the PR as
`requires_human_ack`:

- The PR cannot be merged until a designated reviewer (admin /
  maintainer team) leaves an approving review specifically
  acknowledging the override.
- The override + ack appear in the merged commit's trailer for
  audit.

This is a DECISION, not a recommendation (review round 7
correction). Overrides should be rare and visibly approved.

Override usage example:
```
fix(contracts): emergency hotfix for v1 silent-fail

PHASE_OVERRIDE: production incident requires v1 patch while
v2 is still in phase 1 build-out.
```

### 6.5 `.v2-current-phase` file

Single line containing the current phase number, e.g. `1`. Lives
at repo root. Bumped in the same commit that completes a phase
(usually alongside the phase-completion tag).

---

## 7. Rollback / tag plan

### 7.1 Tag points

- `pre-contracts-v2-2026-05-14` — already on `origin`. Ultimate
  revert: every v2 change can be undone by checking out this tag.
- `v2-phase-1-complete` — annotated tag created at end of phase 1
  if all tests pass.
- `v2-phase-N-complete` — same pattern per phase.
- `v2-ready-for-v1-removal` — after phase 8 (first end-to-end
  fire). Marks the boundary at which v1 edits become permissible.

### 7.2 Per-phase revert procedure

For phase 1 specifically:

1. `git checkout pre-contracts-v2-2026-05-14`
2. `git checkout -b revert-v2-phase-1-<date>`
3. Verify CI passes (only v1 tests run; v2 dirs absent).
4. Open revert PR; merge after review.

Because phase 1 only adds new files under `app/v2/` and
`tests/v2/`, a revert is also achievable via:

```
git rm -rf app/v2/ tests/v2/
git restore -s pre-contracts-v2-2026-05-14 -- scripts/check_phase_scope.py .githooks/v2_phase_guard.sh .github/workflows/v2_phase_guard.yml
```

(directories didn't exist at the baseline tag.)

### 7.3 Production safety

Phase 1 ships zero runtime code. Production cannot regress from
phase 1's merge because:

- No new code path runs in production.
- The v001 migration is NOT auto-applied to production DB at this
  phase. Migration runner exists but is called only by tests.
- v1 code paths untouched.

### 7.4 Forward-only tag policy

Tags are never moved. If phase 1 needs a fix, a new commit lands
+ a new tag `v2-phase-1-fix-<n>-complete` is created. The
original `v2-phase-1-complete` tag stays.

---

## 8. Acceptance criteria for phase 1 completion

Phase 1 is considered complete when ALL of these are true:

1. All files in §2 exist.
2. `uv run python -m pytest tests/v2/ --tb=short -q` passes with
   zero failures.
3. `uv run python -m pytest tests/` (full suite) still passes —
   v1 tests untouched.
4. `uv run python scripts/check_phase_scope.py --diff pre-contracts-v2-2026-05-14...HEAD`
   reports no violations.
5. `apply_pending(fresh_conn)` against a clean SQLite DB
   completes; subsequent `apply_pending` is a no-op.
6. Every transition in §4.0.1 of the design contract has at
   least one passing test in `test_lifecycle_transitions.py`.
7. Every invariant in §4.0.4 has at least one negative-case
   passing test in `test_invariants.py`.
8. Every EventKind in the canonical list has at least one
   round-trip test in `test_models_event.py`.
9. CI workflow file `.github/workflows/v2_phase_guard.yml`
   passes against itself when the phase-1 PR opens.
10. Annotated git tag `v2-phase-1-complete` is created and
    pushed.

---

## 9. Decisions resolved by review

The following choices are now DECIDED (no longer open).

- **DB target**: phase 1 uses test DBs only.
  `apply_pending(conn)` takes an explicit connection. No implicit
  `data/ori-scheduler.db` access in phase 1. See §3.3.
- **CI override approval**: `PHASE_OVERRIDE:` requires explicit
  human ack on the PR (designated reviewer team), not just
  annotation. See §6.4.
- **Commit granularity**: phase 1 lands as several small commits
  inside the
  `pre-contracts-v2-2026-05-14`..`v2-phase-1-complete` range, not
  one giant commit. Suggested grouping (reviewer-approved
  ordering — first commit is the smallest, building outward):
  1. **enums + common models + triggers + matching tests**
     (the first commit; smallest scope to verify the foundation)
  2. ScheduleSpec + ExecutionPlan models + matching tests
  3. Run + Event + State + Snapshot models + matching tests
  4. DDL + migration runner skeleton + migration tests
  5. CI guard scripts + hooks + workflow + guard tests
  Test-first invariant from design §11.3 — every model commit
  ships with its test file in the SAME commit. CI guard
  tolerates multi-commit phases as long as every commit's
  staged files are phase-1-allowlisted.
- **Source mode collapsed to 3**: `version_pinned` is gone;
  versioned-pin lives inside `LiveSourceRef`. Phase-1 enum
  reflects this. See §4.1.
- **RecoveryPolicy enum renamed**: values are now
  `queue_retry | mark_failed | clear_claim`, dropping the dead
  `retry_pending` term.
- **Trigger discriminator union completeness**: phase 1
  declares all 5 trigger subclasses as full Pydantic classes;
  phase-3 trigger validators (`IntervalTrigger`,
  `ConditionalTrigger`, `EventTrigger`) are present but empty.
  Avoids reshuffling the union when phase-3 lands.
- **`Event.payload` shape**: stays as `dict` in phase 1. Phase 2
  introduces per-kind Pydantic payload models. Deferral
  confirmed acceptable.
- **`pyproject.toml` is untouched in phase 1.** No Pydantic
  pin, no dep additions. Repo already imports Pydantic v2 via
  `app/contracts/schema.py`; explicit pinning would mean
  pyproject + uv.lock churn that doesn't belong in phase 1.
  Dependency hygiene happens later. The phase guard
  allowlist correctly excludes `pyproject.toml` for phase 1.
- **`pytest-asyncio` already present** at `>=0.23.0` per the
  current `pyproject.toml`. No dep change required.
- **Phase tag annotation content** for `v2-phase-1-complete`:

  ```
  v2 phase 1 complete

  Adds schema-only scheduling v2 foundation:
  ScheduleSpec, ExecutionPlan, Run, EventLedger, state/snapshot models,
  SQLite DDL/migration skeleton, lifecycle/invariant tests, CI phase guard.

  No runtime scheduler changes. v1 paths untouched.
  Design: docs/CONTRACTS_V2_DESIGN.md
  Plan: docs/PHASE_1_PLAN.md
  ```

## 10. No open questions remain

All decisions captured in §9. Phase-1 implementation may proceed
once the user gives the explicit go signal.

---

## 11. Vocab for the reviewer

If you flag any of the following, that's useful signal:

- File path choices that lock us into awkward refactors later.
- Pydantic model field names that conflict with stdlib / Pydantic v2 reserved words.
- Tests I haven't planned that would have caught past bugs (e.g. the round-6 idempotency-key-attempt regression).
- CI guard logic that's too permissive or too strict.
- Migration strategy gotchas (in-flight DB, rollback semantics, concurrent migrations).
- Anything missing from the acceptance criteria that should block phase 1 completion.

No implementation begins until this plan is approved.
