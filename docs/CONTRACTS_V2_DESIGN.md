# Contracts v2 — foundation redesign

> **DESIGN ONLY — NO IMPLEMENTATION UNTIL REVIEWED.**
>
> This document is the rewritten foundation plan after five review
> rounds. Safety tag `pre-contracts-v2-2026-05-14` captures the
> pre-redesign state for revert. No code lands until this doc is
> approved.

---

## 1. Problem statement

V1 contracts (today's `app/contracts/`) make `Contract` the root
object. A single Pydantic shape carries seven concerns at once:
trigger, owner, source inputs, execution plan, delivery policy,
failure policy, audit policy. Authoring is LLM-driven freeform
JSON. Every revision is a fresh chance for the LLM to hallucinate
field names, types, channel IDs, or step structure.

Two months of incremental patches (validators, status checks,
admin alerts, audit mirror, prefix blocks) closed individual bug
classes but did not narrow the authoring surface. The system still
behaves as a one-off-per-incident hardening exercise instead of a
production-grade scheduler.

v2 makes the production design explicit:

- **ScheduleSpec is the root.** It carries scheduling, ownership,
  delivery, and failure concerns. Simple reminders are full
  ScheduleSpecs with no `execution_plan`.
- **ExecutionPlan (formerly Contract) is the optional frozen
  workflow body** referenced by ScheduleSpecs that need
  hash-pinned multi-step execution (FBA audit, recurring series).
- **Run is the unit of execution.** Each due fire creates a Run
  row. Workers claim runs by lock/CAS. Retries advance the same
  Run's attempt sequence. Replay re-creates Runs from history.
- **EventLedger is the single audit truth.** Every state
  transition writes an event row. No parallel JSONL / failures
  log / snapshot file fragmentation.
- **APScheduler is wakeup-only.** It calls a wakeup function that
  computes due times and inserts Run rows. APScheduler holds no
  business state.

User-facing naming: "scheduled task" / "reminder" / "series" /
"digest". "Contract" stays as engineering language for the frozen
ExecutionPlan piece.

## 2. Core invariant

**One execution engine. One validation path. One audit path.**

Every authoring surface (typed ADK tools, templates, source
importers, CustomFlow) MUST produce a ScheduleSpec that funnels
through a single `validate_schedule_spec → freeze` chokepoint.
Every fire creates a Run that goes through a single worker claim
path. Every state transition writes to the EventLedger. No
parallel infrastructure. Templates are facades over the typed tool
registry, not a second scheduler.

## 3. Use cases v2 must cover

| # | Use case | Trigger | Body | Side effects |
|---|---|---|---|---|
| 1 | Daily Linux tutoring lesson | cron | ExecutionPlan (source + format) | slack_post |
| 2 | Weekly news digest | cron | ExecutionPlan (multi-source + LLM summary) | slack_post / drive_doc_fill |
| 3 | Daily spreadsheet update from BigQuery | cron | ExecutionPlan (BQ loader + reasoning) | sheet_append |
| 4 | Drive doc weekly refresh | cron | ExecutionPlan (compose body) | drive_doc_fill |
| 5 | YouTube watch + summary | one-off / event | ExecutionPlan (video summarize) | slack_post + drive_doc_fill |
| 6 | One-off reminder | one-off | (no ExecutionPlan) | telegram_dm OR slack_post |
| 7 | Recurring "did you do X" follow-up | cron with conditional skip | ExecutionPlan (source check) | telegram_dm |
| 8 | Inventory alert | conditional / interval | ExecutionPlan | slack_post + memory_update |
| 9 | Multi-step ASIN audit (FBA) | cron | ExecutionPlan (6 reasoning steps) | sheet_append + slack_post |
| 10 | News digest delivered to multiple channels | cron | ExecutionPlan | slack_post (×N) |
| 11 | Conversational follow-up ("ping me in 2h to check") | one-off | (no ExecutionPlan) | telegram_dm into origin session |
| 12 | Series with cross-fire state | cron + state | ExecutionPlan (state_read + pick + state_write) | slack_post |
| 13 | Snoozable reminder | one-off with snooze | (no ExecutionPlan) | telegram_dm + interactive snooze |
| 14 | Cross-platform: created via Telegram, delivers to Slack | cron | ExecutionPlan (any) | slack_post |
| 15 | Event-triggered (new email, calendar) | event | ExecutionPlan | telegram_dm / slack_post |

Use cases 6, 11, 13 are **lightweight** — ScheduleSpec only, no
ExecutionPlan. The rest require an ExecutionPlan.

---

## 4. Top-level architecture

### 4.0 Primitives

```
ScheduleSpec ──────► ExecutionPlan (optional, hash-pinned)
     │                    │
     ▼                    ▼
   Run ◄──────────── claim by worker
     │
     ▼
EventLedger (single source of audit truth)
```

#### ScheduleSpec

The root, persisted as the user's intent.

```
ScheduleSpec(
    id: str                    # snake_case, unique
    owner: User                 # who scheduled it
    description: str            # human-readable
    trigger: Trigger            # cron / one_off / interval / event / conditional
    delivery: Delivery          # target session, fallback policy
    failure: FailurePolicy      # on_failure action, retry policy
    audit: AuditPolicy          # retention, snapshot limits
    status: ScheduleStatus      # active | paused | archived
    execution_plan_hash: Optional[str]
        # references a frozen ExecutionPlan row when the schedule
        # has structured work to do. None for simple reminders.
    template: Optional[TemplateRef]
        # name + version of the template that produced this spec,
        # if any. None for CustomFlow.
    authored_at, parent_hash, hash
)
```

#### ExecutionPlan (the artifact formerly known as Contract)

Hash-pinned frozen workflow body. Only present for ScheduleSpecs
that need multi-step execution.

```
ExecutionPlan(
    id: str                     # matches a ScheduleSpec's execution_plan_hash via hash
    inputs: list[InputSpec]     # loaders (deterministic data fetch)
    reasoning: list[ReasoningStep]
    emit: list[EmitStep]
    on_failure: FailureAction   # step-level (overrides ScheduleSpec.failure if present)
    enforcement: EnforcementMode  # always STRICT for production
    hash: str
)
```

ExecutionPlans are stored once and may be referenced by multiple
ScheduleSpecs (deduplicated by hash). When a ScheduleSpec is
revised, a new ExecutionPlan may be hash-linked while old fires
keep firing the prior ExecutionPlan until the schedule itself is
re-scheduled to the new hash.

#### Run

One due execution. The unit of work for the worker pool.

```
Run(
    id: uuid4
    schedule_id: str            # FK ScheduleSpec.id
    execution_plan_hash: Optional[str]
        # snapshot of ScheduleSpec.execution_plan_hash at creation time;
        # subsequent ScheduleSpec revisions DO NOT affect this run.
    fire_reason: enum           # scheduled | manual | retry | replay | backfill
    due_at: datetime            # when the run should fire (UTC)
    status: RunStatus           # pending | claimed | running | succeeded | failed | cancelled
    attempt: int                # 1 for first try, 2+ for retries
    root_run_id: uuid4
        # stable id across the retry chain. For first-attempt runs:
        # root_run_id = id (self-reference). For retries: root_run_id
        # = original first-attempt's id. Used for retry-chain queries,
        # emit idempotency keys, replay lineage, and user-visible
        # grouping ("show me all 3 attempts of yesterday's 1pm fire").
    parent_run_id: Optional[uuid4]
        # previous attempt's id in the retry chain; null for the
        # first attempt.
    claimed_by: Optional[str]   # worker id (diagnostic; not enforced authority)
    claimed_at: Optional[datetime]
    started_at, completed_at: Optional[datetime]
    error: Optional[str]
)
```

#### EventLedger

Append-only audit ledger. Every state transition writes a row.

```
Event(
    id: uuid4
    run_id: Optional[uuid4]     # null for ScheduleSpec-level events
    schedule_id: str
    ts: datetime
    kind: EventKind             # see lifecycle below
    payload: JSON               # event-specific fields
    correlates: Optional[uuid4]
        # links related events (e.g. ack <-> admin_alert_sent)
)
```

EventKinds (canonical list — phase-1 set):

```
schedule_created
schedule_revised
schedule_paused
schedule_archived
schedule_resumed
schedule_revived
run_created
run_claimed
run_started
run_recovered                  # boot recovery promoted a stale claimed/running run
source_resolved
source_drift_detected
source_failed
reasoning_started
reasoning_completed
reasoning_failed
emit_started
emit_succeeded
emit_failed
emit_skipped_idempotent        # adapter short-circuited; prior success row exists for same idempotency_key
delivery_failed
admin_alert_sent
admin_alert_acked
run_succeeded
run_failed
run_retry_scheduled            # written when a failed run spawns a new retry Run
run_cancelled
audit_mirror_appended          # bot's session received a model-role event for a contract emit
boot_self_test_passed
boot_self_test_failed
migration_v1_to_v2_complete    # one-time schedule-level event after migration runner finishes
```

### 4.0.1 Lifecycle + state transitions

RunStatus values: `pending | claimed | running | succeeded | failed | cancelled`.
**No `retry_pending`** — retries are implemented as new pending
Run rows with `fire_reason=retry` and a `root_run_id` link to the
original Run (per review round 6 correction).

```
[no run]
    │  wakeup detects schedule due at T
    │  OR failure handler schedules a retry
    ▼
 pending  (Run row + run_created event written in same TX)
    │  worker claims (UPDATE WHERE status=pending RETURNING)
    ▼
 claimed  (claimed_at set, run_claimed event)
    │  worker calls execute_run()
    ▼
 running  (started_at set, run_started event)
    │
    ├──► succeeded  (run_succeeded event, completed_at set)
    │
    ├──► failed  (run_failed event, completed_at set, error set)
    │       │  on_failure policy = retry?
    │       │  if YES → insert new pending Run with
    │       │  fire_reason=retry, root_run_id, parent_run_id,
    │       │  due_at=now+backoff, and write
    │       │  run_retry_scheduled event correlating the new run
    │       │  to the failed one.  Old run stays terminal at
    │       │  failed.
    │       ▼
    │   (terminal)
    │
    └──► cancelled  (run_cancelled event; user-initiated or admin)
```

**Full transition table:**

| From | To | Initiator | Event(s) written | Side effects |
|---|---|---|---|---|
| (none) | `pending` | wakeup function | `run_created` | Run row inserted; `root_run_id = id`, `fire_reason ∈ {scheduled, backfill, manual, replay}` |
| (none) | `pending` | failure handler (retry path) | `run_created`, `run_retry_scheduled` | New Run row with `fire_reason=retry`, `root_run_id` = original first-attempt's id, `parent_run_id` = previous attempt's id, `due_at = now + backoff` |
| `pending` | `claimed` | worker pool | `run_claimed` | claimed_by, claimed_at set |
| `pending` | `cancelled` | admin / `schedule_pause`+policy / `schedule_archive` | `run_cancelled` | — |
| `claimed` | `running` | worker | `run_started` | started_at set |
| `claimed` | `pending` | boot recovery (claim_timeout elapsed) | `run_recovered` | claimed_by/claimed_at cleared; new claim attempt allowed |
| `claimed` | `failed` | boot recovery (per `RecoveryPolicy.MARK_FAILED`) | `run_recovered`, `run_failed` | terminal |
| `running` | `succeeded` | worker after emit ok | `run_succeeded`, `emit_succeeded` (×N) | completed_at set |
| `running` | `failed` | worker after exception | `run_failed`, `emit_failed` (×?), `error` set | completed_at set |
| `running` | `failed` | boot recovery (per `RecoveryPolicy.MARK_FAILED`) | `run_recovered`, `run_failed` | terminal; may trigger retry insertion per `on_failure` policy |
| `failed` | (terminal) | failure handler if no retry / `abort=true` | `admin_alert_sent` (if action=alert_admin) | — |
| any non-terminal | `cancelled` | admin / `schedule.status=archived` | `run_cancelled` | — |

`succeeded` / `failed` / `cancelled` are terminal — once written,
the row never transitions again. A retry is a NEW Run, not a state
change on the old one.

### 4.0.2 SQLite storage schemas

Single SQLite database `data/ori-scheduler.db` (same file
APScheduler already uses; add new tables alongside its existing
`apscheduler_jobs`).

```sql
-- ScheduleSpec
CREATE TABLE schedules (
    id              TEXT PRIMARY KEY,           -- snake_case, unique
    owner           TEXT NOT NULL,              -- e.g. 'tg_330959414', 'sergey@mellanni.com'
    description     TEXT NOT NULL,
    trigger_json    TEXT NOT NULL,              -- serialized Trigger union
    delivery_json   TEXT NOT NULL,              -- serialized Delivery
    failure_json    TEXT NOT NULL,              -- serialized FailurePolicy
    audit_json      TEXT NOT NULL,              -- serialized AuditPolicy
    status          TEXT NOT NULL CHECK (status IN ('active', 'paused', 'archived')),
    execution_plan_hash TEXT,                   -- FK execution_plans.hash (nullable)
    template_json   TEXT,                       -- {name, version} or null
    authored_at     TEXT NOT NULL,
    parent_hash     TEXT,                       -- for revisions
    hash            TEXT NOT NULL UNIQUE        -- SHA-256 of canonical body
);

CREATE INDEX idx_schedules_status ON schedules(status);
CREATE INDEX idx_schedules_owner ON schedules(owner);
CREATE INDEX idx_schedules_plan_hash ON schedules(execution_plan_hash);

-- ExecutionPlan
CREATE TABLE execution_plans (
    hash            TEXT PRIMARY KEY,           -- SHA-256 of plan body
    body_json       TEXT NOT NULL,              -- full Pydantic-serialized ExecutionPlan
    enforcement     TEXT NOT NULL CHECK (enforcement IN ('strict', 'permissive')),
    authored_at     TEXT NOT NULL,
    author          TEXT NOT NULL
);
-- ExecutionPlans are immutable; no UPDATE, only INSERT and SELECT.

-- Run
CREATE TABLE runs (
    id                  TEXT PRIMARY KEY,        -- uuid4
    schedule_id         TEXT NOT NULL,           -- FK schedules.id
    execution_plan_hash TEXT,                    -- snapshot at run creation
    fire_reason         TEXT NOT NULL CHECK (fire_reason IN ('scheduled', 'manual', 'retry', 'replay', 'backfill')),
    due_at              TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt             INTEGER NOT NULL DEFAULT 1,
    root_run_id         TEXT NOT NULL,           -- stable across the retry chain; first attempt's row sets root_run_id = id (self-ref)
    parent_run_id       TEXT,                    -- previous attempt in the chain; null for first attempt
    claimed_by          TEXT,
    claimed_at          TEXT,
    started_at          TEXT,
    completed_at        TEXT,
    error               TEXT,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id),
    FOREIGN KEY (execution_plan_hash) REFERENCES execution_plans(hash),
    FOREIGN KEY (root_run_id) REFERENCES runs(id),
    FOREIGN KEY (parent_run_id) REFERENCES runs(id)
);

CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_due_at ON runs(due_at);
CREATE INDEX idx_runs_schedule_due ON runs(schedule_id, due_at);
CREATE INDEX idx_runs_pending_due ON runs(status, due_at) WHERE status = 'pending';
CREATE INDEX idx_runs_root ON runs(root_run_id);  -- O(1) retry-chain lookup
CREATE INDEX idx_runs_schedule_running ON runs(schedule_id, status) WHERE status IN ('claimed', 'running');  -- single-flight check

-- EventLedger
-- kind is CHECK-constrained against the canonical EventKind set
-- (app/v2/enums.py:EventKind). Adding a new EventKind requires a
-- new migration to widen this CHECK alongside the enum addition.
CREATE TABLE events (
    id              TEXT PRIMARY KEY,            -- uuid4
    run_id          TEXT,                        -- FK runs.id (nullable for schedule-level events)
    schedule_id     TEXT NOT NULL,
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN (
                        'schedule_created', 'schedule_revised', 'schedule_paused',
                        'schedule_archived', 'schedule_resumed', 'schedule_revived',
                        'run_created', 'run_claimed', 'run_started', 'run_recovered',
                        'run_succeeded', 'run_failed', 'run_retry_scheduled', 'run_cancelled',
                        'source_resolved', 'source_drift_detected', 'source_failed',
                        'reasoning_started', 'reasoning_completed', 'reasoning_failed',
                        'emit_started', 'emit_succeeded', 'emit_failed', 'emit_skipped_idempotent',
                        'delivery_failed', 'admin_alert_sent', 'admin_alert_acked',
                        'audit_mirror_appended', 'boot_self_test_passed',
                        'boot_self_test_failed', 'migration_v1_to_v2_complete'
                    )),
    payload_json    TEXT NOT NULL,               -- event-specific fields
    correlates      TEXT,                        -- another event id (e.g. ack <-> admin_alert_sent)
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (schedule_id) REFERENCES schedules(id)
);

CREATE INDEX idx_events_run ON events(run_id);
CREATE INDEX idx_events_schedule_ts ON events(schedule_id, ts);
CREATE INDEX idx_events_kind ON events(kind);

-- Cross-fire state (per-schedule key/value with versions)
-- written_by_run is a nullable FK to runs(id): author-time seeds
-- legitimately have NULL, but when set the value must reference
-- a real Run (replay + lineage queries depend on this).
CREATE TABLE schedule_state (
    schedule_id     TEXT NOT NULL,
    key             TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,  -- bumped on each write
    written_at      TEXT NOT NULL,
    written_by_run  TEXT,                        -- run id (nullable for author-time seeds)
    PRIMARY KEY (schedule_id, key),
    FOREIGN KEY (schedule_id) REFERENCES schedules(id),
    FOREIGN KEY (written_by_run) REFERENCES runs(id)
);

-- Source snapshots (when contracts use LiveSourceRef)
-- Stored on filesystem for size, indexed in SQLite by content hash.
-- selection_method records HOW the fire-time item was picked so
-- the audit log can explain "why item 5 instead of item 12".
-- CHECK-constrained against SelectionMethod (app/v2/enums.py).
CREATE TABLE source_snapshots (
    run_id           TEXT NOT NULL,
    source_id        TEXT NOT NULL,              -- input_id from ExecutionPlan
    content_hash     TEXT NOT NULL,
    content_path     TEXT NOT NULL,              -- relative path on disk
    content_size     INTEGER NOT NULL,
    fetched_at       TEXT NOT NULL,
    source_kind      TEXT NOT NULL,
    source_version   TEXT,                       -- revision id where supported
    selection_method TEXT NOT NULL CHECK (selection_method IN (
                         'stable_id', 'content_hash', 'row_number'
                     )),
    PRIMARY KEY (run_id, source_id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX idx_snapshots_hash ON source_snapshots(content_hash);
```

Filesystem still holds large objects (source snapshot files,
contract failure raw text), keyed by hash in SQLite. WAL mode for
SQLite (`PRAGMA journal_mode=WAL`) for concurrent reader / single
writer.

### 4.0.3 APScheduler scope = wakeup-only

APScheduler keeps managing only its existing job-store table
(`apscheduler_jobs`). Each scheduled job has a single callback:
`wakeup(schedule_id)`. The callback:

1. Reads the ScheduleSpec from `schedules` table.
2. Verifies status is `active` (paused/archived → no-op).
3. Computes the next due_at(s) — for cron, the current fire time;
   for interval / backfill, possibly multiple.
4. For each due_at, INSERTs a Run row with status=`pending` in
   the same transaction as a `run_created` event row.
5. Returns. The worker pool picks up the pending runs.

APScheduler never decides retry semantics, never holds run state,
never reads the event ledger. It is a wakeup primitive.

### 4.0.4 Phase 1 invariants

These invariants hold across all execution paths and must be
encoded as schema constraints, transaction boundaries, and
recovery logic.

| Invariant | Mechanism |
|---|---|
| **Run claim** | Exactly one worker transitions `pending → claimed` via `UPDATE runs SET status='claimed', claimed_by=?, claimed_at=? WHERE id=? AND status='pending' AND NOT EXISTS (SELECT 1 FROM runs r2 WHERE r2.schedule_id = runs.schedule_id AND r2.status IN ('claimed', 'running'))`. The `RETURNING` clause confirms ownership. The single-flight subquery prevents two workers from running the same schedule concurrently — if blocked, run stays at `pending` (NOT stuck at `claimed`) and the next worker tick retries. SQLite serializes writes via WAL single-writer. |
| **Retry chain** | A failed Run is terminal at `status='failed'`. If `on_failure.action='retry_later'` and `attempt < max_retries`, the failure handler INSERTs a new Run row with `fire_reason='retry'`, `root_run_id = old.root_run_id`, `parent_run_id = old.id`, `attempt = old.attempt + 1`, `due_at = now + backoff(old.attempt)`, `status='pending'`. The old Run is never re-statused. Retry chain queryable in O(1) via `WHERE root_run_id = ?`. |
| **Emit idempotency** | Stable across retries. `idempotency_key = f"{schedule_id}:{root_run_id}:{emit_id}"` where `emit_id` is the EmitStep's required stable identifier (NOT the run-local `attempt`, which would change on retry and defeat dedup). Adapter checks the event ledger for prior `emit_succeeded` rows with the same key; refuses duplicate. Where the destination supports a native dedup token (e.g. Slack `client_msg_id`), the idempotency key is passed through as that token too. |
| **Ledger transactionality** | State transition + event row written in the same SQLite transaction. SQLite gives atomic multi-statement TX. No risk of "state moved but event missed" or vice versa. |
| **Recovery on boot** | A boot-time scan executes: `SELECT id FROM runs WHERE status IN ('claimed', 'running') AND claimed_at < now - timeout`. Each is routed by policy: `RecoveryPolicy.RETRY_PENDING` (default for `running`), `RecoveryPolicy.MARK_FAILED` (default for `claimed`), or `RecoveryPolicy.RESUME` (manual override only). |
| **Backfill** | The wakeup function for cron-style triggers also computes any missed `due_at` values within `backfill_policy.window` and inserts Runs for each. Bounded by max-rows-per-wakeup. |
| **Pause** | ScheduleSpec.status=`paused` → wakeup function returns no-op. Already-pending Runs are handled per `paused_pending_policy` (`let_complete` / `cancel_pending`). Already-running Runs are not interrupted. |
| **Archive** | ScheduleSpec.status=`archived` → wakeup function returns no-op AND existing pending Runs are cancelled. Resuming requires admin-gated `schedule_revive` tool which transitions back to `paused` (not directly to `active`) so the admin must re-approve. |

### 4.0.5 Crash / restart recovery

What happens at each failure point:

**Worker crash (SIGKILL / hardware fault) during `claimed`:**
- Run row stays at `status='claimed'` with `claimed_at` from before crash.
- No `run_started` event was written (worker hadn't transitioned yet).
- Boot recovery scan picks it up; routes by `RecoveryPolicy` for
  `claimed` runs (default: `MARK_FAILED`).

**Worker crash during `running`:**
- Run row at `status='running'`, `started_at` set, no `completed_at`.
- Some `emit_started`/`emit_succeeded`/`emit_failed` events may
  exist depending on how far the worker got.
- Boot recovery routes by `RecoveryPolicy` for `running` runs
  (default: `RETRY_PENDING` → new attempt is queued; emit
  idempotency prevents double-delivery for emits that already
  succeeded).

**Worker crash between emit-succeeded HTTP call and event-written:**
- The emit transmitted to Slack / Telegram / etc, but no
  `emit_succeeded` event landed.
- On retry, the idempotency check finds no prior success row →
  the emit will RE-FIRE.
- Mitigation: every adapter's HTTP call MUST be preceded by an
  `emit_started` event with the idempotency key. On retry, finding
  an `emit_started` without a matching `emit_succeeded` triggers a
  duplicate-check at the destination (Slack API supports
  `client_msg_id` for chat.postMessage; reuse our idempotency key).
  For destinations without native dedup (raw httpx send),
  document the at-most-once-vs-at-least-once tradeoff and pick
  per-adapter.

**SQLite WAL recovery:**
- WAL mode handles partial-write recovery transparently at the next
  open. `PRAGMA wal_checkpoint(FULL)` runs on graceful shutdown to
  minimise replay on next boot.

**Bot process crash before APScheduler persisted next_run_time:**
- APScheduler's SQLAlchemyJobStore is itself in `data/ori-scheduler.db`
  with WAL. The boot resumption replays from the last committed
  state. `misfire_grace_time` handles fires missed during downtime
  (up to 1h by default). Beyond that, backfill policy kicks in for
  affected schedules.

**Partial source-snapshot write:**
- Snapshot row is written to `source_snapshots` table only AFTER
  the on-disk file write completes. A crash mid-write leaves an
  orphan file with no row → garbage-collected by background sweep.
  No row referencing missing file is possible.

**Boot self-test failure:**
- Admin alert self-test sends a Telegram DM via direct httpx
  WHILE the scheduler is still paused (resumed only on
  self-test success).
- Failure writes `boot_self_test_failed` event. Scheduler stays
  paused. Bot refuses to create new Runs from any wakeup until
  self-test passes (manual or periodic retry).

**Database corruption:**
- SQLite integrity_check runs on boot. Hard fail with admin alert
  via direct httpx (the only path that doesn't depend on the DB
  being healthy). Bot refuses to schedule anything until restored
  from backup.

---

## 5. Authoring surface

### 5.1 Typed ADK tools (agent-facing)

Bot's only authoring verbs are typed ADK tools. The LLM never
writes Python builder code or freeform JSON.

Phase-1 tool list (illustrative, not exhaustive):

- `schedule_draft_start(id, description, owner) → draft_id`
- `schedule_set_cron(draft_id, cron_expr, timezone)`
- `schedule_set_one_off(draft_id, at_iso_datetime, timezone)`
- `schedule_set_delivery(draft_id, target_session_id, fallback_policy)`
- `schedule_set_failure_policy(draft_id, on_failure_action, retry_policy)`
- `schedule_attach_execution_plan(draft_id, plan_hash | plan_draft_id)`
- `plan_draft_start(id, author) → plan_draft_id`
- `plan_add_input_bigquery(plan_draft_id, input_id, sql)`
- `plan_add_input_source_drive_file(plan_draft_id, input_id, drive_file_id, ...)`
- `plan_add_reasoning(plan_draft_id, step_id, entry_agent, user_template, output_json_schema)`
- `plan_add_emit_slack_post(plan_draft_id, channel, content, thread_ts=None)`
- `plan_add_emit_sheet_append(plan_draft_id, spreadsheet, range_, row)`
- `schedule_dry_run(draft_id, mode, as_of_datetime=None, mocked_inputs=None)`
- `schedule_freeze(draft_id)` — refuses unless recent dry-run handshake exists
- `schedule_pause(schedule_id)`, `schedule_resume(schedule_id)`, `schedule_archive(schedule_id)`, `schedule_revive(schedule_id)`

Each tool's parameters Pydantic-validate at call time. `channel: Channel`
is a registered enum value, not a string. Unknown enum value →
tool refuses with the list of known options.

### 5.2 Template-first authoring

Bot's DEFAULT path is template selection, not custom composition.

Phase-1 templates (thin facades over the typed-tool registry,
funnel through the same validation pipeline):

- **Stage A (no source dependency)**:
  - `OneOffReminder(at, recipient, text, snoozable=False)`
    — covers use cases 6, 11, 13. Produces a ScheduleSpec with no
    ExecutionPlan.
- **Stage B (after source loaders land)**:
  - `RecurringSeriesFromSource(source, channel, hour_local, timezone, progress_strategy)`
    — covers use cases 1, 12. **Shipped phase 11 (§12
    step 11A)**: STATELESS strategies only (`whole` /
    `skip_unchanged`). Authored through the SAME pipeline
    as `OneOffReminder` — the shared spine was EXTENDED
    additively (§0.3, reviewer-ratified): the
    freeze/commit trigger gate admits `{one_off, cron}`
    and `commit` persists `insert_execution_plan` +
    `insert_schedule` atomically in ONE transaction
    (both-or-neither). Stateful "next unread item"
    progress is deferred to 11B (needs step-13 cross-fire
    `schedule_state`).
  - `ChannelDigest(sources, channel, schedule, summary_prompt)`
    — covers use cases 2, 10. **Deferred to §12 step 11B**
    (needs the LLM reasoning-chain executor for
    `summary_prompt`).

- **CustomFlow** — escape hatch; full typed-tool authoring; lands
  alongside the tool registry.

Templates compile to a ScheduleSpec (+ optional ExecutionPlan) via
the same internal pathway as CustomFlow. Same Pydantic validation,
same dry-run handshake, same diff, same freeze, same audit.

**Per-template payload — `TemplateRef.args`** (added 2026-05-15
in the phase-9 plan landing per `docs/PHASE_9_PLAN.md` §0):
emit-only templates (e.g. `OneOffReminder`) need a place to carry
per-instance payload (the reminder text) without an
ExecutionPlan. `TemplateRef` therefore carries an optional
`args: Optional[dict[str, JsonValue]] = None` field. The type is
`pydantic.types.JsonValue` (recursive JSON-primitive union) so
non-JSON values cannot leak in and break the canonical hash.
`ScheduleSpec.compute_hash` includes `template.args` in its
JSON-serialised payload so a body change re-hashes; pre-amendment
specs (where `args=None`) round-trip with no hash drift.
Per-template arg validation runs inside the template builder
(`OneOffReminder.build(at, recipient, text, …)`) BEFORE the
typed-tool flow; the validator at the ScheduleSpec layer treats
`args` as opaque JSON.

Future templates (`WeeklyAuditWithSheetLog`, `ConditionalAlert`,
`YouTubeSummary`) added as patterns crystallize from real demand.
CustomFlow → Template promotion: surface a hint when a CustomFlow
ScheduleSpec is re-authored 3+ times with the same structural
shape.

### 5.3 Source-driven content

Three source modes:

- **LiteralText**: user / prompt bytes frozen directly into the
  ExecutionPlan body.
- **SnapshotSource**: fetch once at author time; freeze the
  content + metadata into the ExecutionPlan body.
- **LiveSourceRef**: freeze the reference + parser + permissions;
  fetch fresh content at fire time. Optional `version="rev-..."`
  pins a specific revision.

Sources are typed loaders following a shared return contract:

```
{
    "content": <opaque content / list / dict>,
    "metadata": {
        "source_kind": "drive_file" | "google_keep" | "sheet" | "slack_thread" | "local_file" | "literal",
        "source_id": "...",
        "fetched_at": "2026-05-14T10:00:00Z",
        "content_hash": "sha256:...",
        "item_count": <int>,
        "source_version": <opaque, optional>,
        "selection_method": "stable_id" | "content_hash" | "row_number",
    }
}
```

Source loaders register via the same `@register` decorator as
other loaders. Phase-1 set:

- `source_literal` — returns LiteralText payload
- `source_drive_file` — Google Docs / Sheets via Drive API
- `source_local_file` — markdown / text / JSON file in an explicit
  allowed-root path (security fence in §5.3.1)
- `source_slack_thread` — read messages in a thread

`source_google_keep` is phase 2+. Keep has a SEPARATE API at
`keep.googleapis.com` with `v1.notes` (not Drive). Loader awaits
the Keep auth flow.

#### 5.3.1 Local-file source security fence

`source_local_file` is the highest-risk source type. Hard
constraints:

- **Allowed roots only**: configurable allowlist; empty by default.
  Admin opt-in per root.
- **No vault / no `data/` runtime files** even if accidentally
  allowlisted: `data/vault*`, `.env`, secrets paths, and
  `data/contract_state/`, `data/ori-scheduler.db`,
  `data/contract_audit/` are explicitly denied.
- **Path canonicalization + containment** (phase-10
  hardening, 2026-05-16 — codex plan-review round-2 🔴):
  the check is **separator-aware**, NOT a string prefix.
  `resolved = Path(p).resolve(strict=True)` (canonicalises
  `..` AND resolves every symlink component); `root =
  Path(allowed_root).resolve(strict=True)`; refuse unless
  `resolved.is_relative_to(root)`. **Do NOT use
  `os.path.realpath(...)` + `startswith(root)`** — string
  prefix admits the sibling-prefix bypass
  (`/safe/root_evil/x` against allowed `/safe/root`).
  `is_relative_to` compares path COMPONENTS so the
  sibling is rejected. Because `resolve()` follows
  symlinks before the check, a symlink whose target
  escapes the root (or points into the deny-list) is
  rejected too — the deny-list check runs on the SAME
  post-`resolve()` path. **Plan↔design consistency is
  load-bearing**: `docs/PHASE_10_PLAN.md` §3.2 and this
  clause MUST state the identical mechanism; the
  phase-10 fence test pins the sibling-prefix +
  symlink-escape + symlink-into-deny-list cases so a
  future drift in either document is caught by CI, not
  by an incident.
- **Max size**: per-file `max_bytes` (default 1 MiB).
  Larger files fail as a NON-fallback typed policy
  failure (`SourcePolicyError`, never served from cache —
  see `docs/PHASE_10_PLAN.md` §3.5) under the snapshot
  `on_oversize` policy.
- **Mime-type allowlist**: text / markdown / json / yaml only by
  default. Binary blocked unless explicitly opted in.

#### 5.3.2 Per-source cache + fallback

Each LiveSourceRef declares its own policy (no global defaults):

- `cache_ttl_seconds`
- `stale_max_age_seconds`
- `fallback_policy ∈ {use_last_good_snapshot, alert_and_skip, alert_and_use_default}`

`alert_and_use_default` valid only when default is user-explicitly
provided AND shown in dry-run. Auth failure NEVER triggers cache
fallback — always routes to on_failure for re-auth.

**Cache-hit vs the non-fallback invariant (phase-10
slice-5 clarification, 2026-05-16 — codex slice-5 🔴).**
`cache_ttl_seconds` is a **cross-fire no-probe window**:
within it the captured snapshot IS the source for the
fire and the loader is NOT invoked (a snapshot-backed
cross-fire cache that re-authorised every hit would not
be a cache). A within-TTL verified `CACHE_HIT` is
therefore legitimate even if the un-invoked live source
would now auth/security-fail — there is no live auth to
mask because no fetch occurs. The "auth failure NEVER
triggers cache fallback" rule above governs the
**post-fetch** decision only (the loader was invoked and
raised). The explicit security control for a source that
must (re)authorize on EVERY fire is
**`cache_ttl_seconds == 0`**: a zero window skips the
cross-fire cache-hit path unconditionally, so the loader
is probed every fire and a live auth/security failure
surfaces every time (never masked by a stale entry).
This clause and `docs/PHASE_10_PLAN.md` §1.4 / §3.3 / §9c
MUST state the identical semantics (plan↔design
consistency is load-bearing); the phase-10 cache test
pins both the ttl>0-no-probe and the ttl==0-always-probe
cases.

#### 5.3.3 Live-source change policy

```
live_change_policy ∈ {
    allow,                              # source edits flow through
    alert_on_shape_change,              # log/alert on content_hash change vs prior snapshot; still fire
    require_reapprove_on_shape_change,  # content_hash change: withhold content, fresh snapshot STILL written as audit; admin re-approves
}
```

**LIVE since phase 11 (§12 step 11A).** The policy is
enforced in the worker fire path: `alert_on_shape_change`
emits the drift event and STILL serves; `require_reapprove_
on_shape_change` withholds content → `FAILED` with the
FRESH snapshot still written as audit (no partial emit).
`RecurringSeriesFromSource`'s `skip_unchanged` strategy
does NOT re-read the snapshot table to detect a change —
it reads the additive derived signal
`ResolveOutcome.changed_vs_prior` the resolver populates
per source provenance (§5.3.5 / `docs/PHASE_11_PLAN.md`
§0.1, §3.3 all-provenance table). Re-reading the table
from the worker would race the resolver's FRESH write —
that was the phase-11 round-1 🔴, structurally avoided by
the derived-signal design.

#### 5.3.4 Item-id strategy

For series-style contracts:

- Sheets / Docs: stable row id / heading anchor / revision-pinned id
- Plain text / markdown / Keep: normalized content hash
- Selection method recorded in audit per fire (`source_resolved` event payload)

#### 5.3.5 Per-fire source snapshot

LiveSourceRef breaks "same plan hash → same output" guarantee.
Per-fire snapshot stored:
- On-disk: `data/contract_audit/<schedule_id>/<run_id>/sources/<source_id>.bin`
  (phase-10, codex plan round-2 🟡 / round-3 🔴 — a RAW
  file holding the §3.6 canonical `content_bytes`
  VERBATIM: no `.json` extension, no JSON envelope, no
  metadata in the file. `sha256(file) == content_hash`
  re-verifies with zero parsing. Identical to
  `docs/PHASE_10_PLAN.md` §3.6 — plan≡design is
  load-bearing.)
- SQLite row in `source_snapshots` keyed by `(run_id, source_id)`,
  pointing at the on-disk `.bin` file via `content_path`;
  ALL metadata (`source_kind`, `selection_method`,
  `source_version`, `fetched_at`, sizes) lives in the
  row, never in the file.

**LIVE since phase 11 (§12 step 11A).** The per-fire
`.bin` + `source_snapshots` row are written by the
resolver/cache path (`resolve_source_cached` on FRESH),
not the worker. The worker is a PURE snapshot CONSUMER:
it never calls `write_snapshot` and never re-reads
`_newest_materialised_snapshot` (Q5 — `docs/PHASE_11_PLAN.md`
§0.1). The only cross-fire signal it consumes is the
derived, race-free `ResolveOutcome.changed_vs_prior`
(§5.3.3). `sha256(.bin) == content_hash` re-verification
and the `cache_ttl_seconds` no-probe (within TTL) /
probe-every-fire (`ttl == 0`) semantics are honoured
end-to-end through the worker without re-implementation.

Retention per ScheduleSpec (the `AuditPolicy` model,
`app/v2/models/common.py`, already a field on
`ScheduleSpec.audit` — there is NO separate retention
model):
- `keep_last_n_snapshots: 30` (default)
- `dedup_by_content_hash: true` (default)
- `redact_fields: [...]`
- `max_snapshot_bytes: 1_000_000`
- `on_oversize ∈ { fail_and_alert, store_pointer_only, redact_and_store, hash_only_no_replay }` — explicit, no silent fallback.

**Retention MECHANISM (phase-10 clarification, 2026-05-16
— codex plan-review round-1 🔴#3).** The
`source_snapshots` table is append-only on the
content-addressed INSERT path (`insert_snapshot`).
Retention is the ONE sanctioned deletion, applied AFTER a
successful snapshot write, never mid-fetch, via a
dedicated `prune_snapshots(conn, *, schedule_id,
source_id, keep_last_n)` helper:
1. Scope rows to `(schedule_id, source_id)` —
   `schedule_id` is resolved by `JOIN runs ON
   runs.id = source_snapshots.run_id` (the table has no
   `schedule_id` column; PK is `(run_id, source_id)`).
2. Order by `fetched_at` DESC, keep the newest
   `keep_last_n`; collect the remaining rows' candidate
   `content_path`s, then DELETE those rows.
3. **COMMIT the row deletes FIRST** (codex plan round-2
   🔴#4 / round-3 🔴 — files are NEVER unlinked inside
   the prune transaction: a rollback would restore rows
   pointing at already-deleted files = data loss).
4. ONLY AFTER commit, on a fresh connection, for each
   collected `content_path` re-query `SELECT 1 FROM
   source_snapshots WHERE content_path = ? LIMIT 1`; if
   no surviving row references it (dedup-shared,
   content-addressed files may be referenced by multiple
   rows), best-effort `unlink`. An orphaned file (row
   gone, file lingers / unlink failed) is harmless and
   is counted + logged for a separate sweep; a deleted
   file with a live row is data loss and this ordering
   makes it structurally impossible.
5. `keep_last_n == 0` prunes every row older than the
   just-written one.
This clarifies — does not contradict — the retention
intent above; the append-only invariant is scoped to
`insert_snapshot`, with `prune_snapshots` the explicit
retention-only exception. **This sequence is verbatim-
identical to `docs/PHASE_10_PLAN.md` §3.4** (plan≡design
is load-bearing — the phase-10 drift class is closed by
keeping these two passages in lock-step; the
`prune_snapshots` test pins the commit-before-unlink
ordering).

#### 5.3.6 Sources are READ-ONLY

Workflows that want to mutate a source MUST go through emit
adapters. Source layer never writes.

#### 5.3.7 Verbatim text preservation

LiteralText and source-imported content store bytes byte-for-byte.
No LLM paraphrase pass anywhere in the authoring flow.

### 5.4 Tool metadata tags

Each registered tool / loader / adapter carries a tag set, not a
binary `mutates: bool`:

- `read_external` (BQ, Sheets read, Keepa)
- `write_external` (Sheets append, Drive write, Slack post, Telegram send)
- `send_message` (user-facing delivery)
- `filesystem_read` (read a file the bot owns on its own host —
  config, draft JSON, cached registry. Distinct from
  `read_external` because the bytes never traverse a third-party
  API. Added 2026-05-15 with phase 7 authoring tools so
  compile / list tools that read draft files have a real tag.
  NOT blocking under read-only reasoning — local introspection
  is safe.)
- `filesystem_write`
- `db_write` (mutates the v2 SQLite store. Added 2026-05-15
  with phase 7 lifecycle tools. Distinct from
  `filesystem_write` because the SQLite path is internal state
  the v2 runtime owns; admin-approval gating differs from
  arbitrary local-file writes. **Blocking under read-only
  reasoning** — any reasoning step with `tool_mode=read_only`
  must NOT mutate the v2 store.)
- `privileged` (admin-only ops)
- `costly` (billable: BQ, LLM calls)
- `uses_oauth`

Policy composition examples:
- Reasoning `tool_mode=read_only` blocks `write_external | send_message | filesystem_write | db_write | privileged`
- Cost-aware authoring warns on `costly`
- Auth-aware loaders compose with `uses_oauth`

Default tag set for new tools: `write_external` (fail-safe).

### 5.5 Single validation entry point

`validate_schedule_spec(spec: ScheduleSpec) → None` is THE
chokepoint. All authoring paths funnel through it:

- ADK tool layer (after each `*_add_*` mutation)
- Template factories (after rendering to ScheduleSpec)
- Source importers (after compiling)
- CustomFlow path (every tool call accumulates a step)
- `schedule_freeze` (final check)
- Worker boot scan (sanity check before claim)

### 5.6 Dry-run handshake + modes

`schedule_freeze` refuses unless `schedule_dry_run` returned ok
within last 60s with the same canonical body hash. Three modes:

- `validate_only`: Pydantic + validators, no I/O. Fast, free.
- `mocked_inputs`: fixture data; real LLM; emit args render.
- `real`: real loaders + real LLM + emit-args render (no actual
  emit fire). Required mode for production cron schedules.

Optional `as_of_datetime`: dry-run pretends it's running at that
date. Validates day-counter / date-math correctness.

`schedule_freeze` records:
- Dry-run hash binding
- Mode + as_of_datetime
- Resolved source metadata (snapshot hash, item count)
- Rendered emit args
- Tool metadata snapshot

### 5.7 Registry cache for channels / sheets / docs

Allowlist enums (`Channel`, `Sheet`, `Doc`) populated from
Slack/Drive APIs but backed by on-disk cache with provenance:

```
data/cache/slack_channels.json
{
    "workspace_id": "T01234",
    "fetched_at": "2026-05-14T08:00:00Z",
    "source": "slack.api.conversations.list",
    "etag": "W/\"abc123\"",
    "channels": [{"id": "C012", "name": "amazon-team"}, ...]
}
```

Boot does NOT block on Slack/Drive reachability. Bot starts with
last-known cache; logs stale warning. Refresh via on-demand tool,
lazy refresh at author time, or background periodic.

**Workspace mismatch detection**: cached workspace_id ≠ current
workspace's → cache invalidated automatically.

**No-cache-and-network-down**: bot boots; existing schedules fire
by stored IDs; new authoring/revise that needs enum lookup is
blocked with a clear error until network returns or cache is
restored.

### 5.8 Diff-confirm on revise + auto-reschedule

`schedule_revise` returns a unified diff of old vs new canonical
body. Bot MUST show diff to user; user replies `approve <hash>`
to commit. Auto-calls `schedule_register_wakeup` (the new
APScheduler-touching step) if previously active.

### 5.9 CustomFlow friction triggers

Admin approval required ONLY when CustomFlow includes:
- `tool_mode=write_allowed` reasoning step
- Dynamic delivery target (loader-resolved channel/user_id)
- Emit count > 3
- Previously-unused adapter
- Tools tagged `privileged` or `costly`
- `filesystem_write` tools
- Live source with `require_reapprove_on_shape_change` + recent shape change

CustomFlow without any of those: standard typed-tool authoring, no
friction beyond dry-run handshake.

---

## 6. Runtime semantics

### 6.1 Worker claim, single-flight per schedule

Worker pool consists of N async tasks. Each loop:

```sql
-- candidate selection
SELECT id, schedule_id, execution_plan_hash, attempt, root_run_id
FROM runs
WHERE status = 'pending' AND due_at <= now
ORDER BY due_at ASC LIMIT 1;

-- atomic claim with single-flight predicate
UPDATE runs
SET status='claimed', claimed_by=?, claimed_at=now
WHERE id = ?
  AND status = 'pending'
  AND NOT EXISTS (
    SELECT 1 FROM runs r2
    WHERE r2.schedule_id = runs.schedule_id
      AND r2.status IN ('claimed', 'running')
  )
RETURNING id, schedule_id, execution_plan_hash, attempt, root_run_id;
```

Three predicates fire atomically:
- `status = 'pending'` — claim race-free (only one worker wins)
- `NOT EXISTS (...)` — single-flight per schedule (another worker
  is already on it) → no claim
- `RETURNING ...` — confirms the win

If no row is returned, the run stays at `pending`. The next
worker tick retries. **No stuck `claimed` rows from single-flight
blocks** (review round 6 correction — leaving runs at `claimed`
made them vulnerable to recovery-timeout misclassification).

The single-flight predicate is broader than APScheduler
`max_instances=1` because it also blocks manual / replay / retry
runs from overlapping a still-running scheduled fire.

### 6.2 Read-only reasoning by default

Worker hard-blocks any reasoning-step tool whose registration
metadata has `write_external | send_message | filesystem_write | privileged`
when the step's `tool_mode == "read_only"` (default). All writes
go through emit adapters.

#### 6.2.1 Context-overflow handling (no silent failures)

Provider context-limit errors are typed failure modes, not
silent retries. When a reasoning step exceeds the model's
context window:

- The provider-specific error (Anthropic
  `BadRequestError: prompt is too long`, Google
  `InvalidArgument: input too long`, OpenAI
  `context_length_exceeded`, etc.) is caught at the LLM
  adapter boundary and surfaced as a typed
  `ContextOverflowError` (or similar) reasoning-failure.
- The Run row transitions to `status='failed'` with
  `error` populated by the typed message.
- The EventLedger receives `reasoning_failed` AND
  `run_failed` events in the same TX as the status flip,
  with payload distinguishing context-overflow from other
  reasoning failures so observability dashboards can
  count them separately.
- **No blind retry of the same oversized input.** The
  retry chain (root_run_id) records the attempt, but a
  retry's first action MUST go through the
  failure-policy chain (alert / truncate / abandon),
  NOT a naive re-fire of the same prompt — which would
  hit the same limit and burn provider quota in a loop.
- Routing: failure surfaces through `on_failure_action`
  per the schedule's `FailurePolicy`. The default
  ALERT_ADMIN path sends a structured admin alert that
  names the schedule, the run id, the offending step,
  and the prompt-size / context-limit ratio so the
  admin can choose between truncation, summarisation
  rerun, or schedule revision.

This contract lands when reasoning-step execution ships in
step 12 (post-renumber). Phase 5's empty-body worker never
hits it; pinning the contract here so the phase-12
implementation lands with the failure handling already
specified rather than as a follow-up patch.

### 6.3 Emit-only side effects

If a ScheduleSpec needs to write external state, the work happens
in an `emit` step with declared frozen args. Reasoning produces
data; emit consumes it. No more "Data Analyst agent records to
sheet" smuggled into the LLM prompt.

### 6.4 Idempotency keys on emit

`idempotency_key = f"{schedule_id}:{root_run_id}:{emit_id}"`.

Three properties that matter:

- **Schedule scope**: distinct schedules with the same `emit_id`
  (e.g. both have a step called `post_slack`) never collide.
- **Stable across retries**: `root_run_id` is constant across the
  retry chain — attempt 1 (run_id=R1, root_run_id=R1) and attempt
  2 (run_id=R2, root_run_id=R1, parent_run_id=R1) share the same
  idempotency key for the same emit. **`attempt` is deliberately
  NOT in the key** (review round 6 correction — including it
  would make retries see "no prior success" and re-post).
- **Emit position-stable**: `emit_id` is a required string on
  every `EmitStep` (snake_case). The key relies on
  position-stable identifiers, not list indices, so reordering
  emits in a revised ExecutionPlan doesn't perturb dedup for
  in-flight retries of the OLD plan. (Different plan hash =
  different root_run_id anyway, so cross-plan collisions are
  prevented at the root_run_id level.)

Adapter behavior: before posting, query the EventLedger for prior
`emit_succeeded` rows with the same `idempotency_key`. If found,
short-circuit and write an `emit_skipped_idempotent` event
(canonical event kind; see §4.0). Otherwise, write `emit_started`,
post, then write `emit_succeeded` with the key in the payload.

For destinations with native dedup tokens (Slack
`client_msg_id`), pass the idempotency key as that token too —
defense-in-depth at the upstream layer.

For destinations without native dedup (raw httpx send to a
webhook), the at-most-once-vs-at-least-once tradeoff is per
adapter. Document the choice in the adapter's docstring.

### 6.5 Cross-fire state with CAS

`schedule_state` table (per-schedule key/value with `version`
column). Two new primitives:

- `state_read(schedule_id, key)` → `{value, version, written_at, written_by_run}` or `None`
- `state_write(schedule_id, key, value, expected_version=None)`
  — atomic write inside a transaction; if `expected_version`
  provided, refuse unless current version matches (CAS).

LLM never touches state. Computed by deterministic loader logic.

### 6.6 Audit-mirror into channel session

After successful emit, worker calls `mirror_emit_to_session(adapter, args, text)`
to append a model-role event into the receiving channel's ADK
session, so bot sees its own scheduled output on next-turn
follow-ups. (Already shipped, carries forward unchanged.)

### 6.7 Boot self-test

Sequence:
1. Scheduler starts, stays PAUSED.
2. Slack/Telegram pollers initialise + register adapters.
3. Admin-alert self-test fires via direct httpx (not contract
   emit). `boot_self_test_passed` or `boot_self_test_failed` event
   written.
4. If passed: scheduler resumes. Wakeup callbacks become active.
5. If failed: scheduler stays paused. Bot refuses to insert any
   new Run rows until admin DM env is corrected.

---

## 7. Failure semantics

### 7.1 on_failure policies

- `alert_admin` (default): admin alert path; ledger event; ack required
- `retry_later`: new Run with `parent_run_id`, `attempt+1`, exponential backoff up to `max_retries`
- `abort_silent`: terminal failure; ledger event but no admin alert
- `custom`: invokes a registered callable

### 7.2 Three-layer delivery fallback (L1/L2/L3)

- **L1**: emit to declared target (e.g. Slack channel)
- **L2 (on L1 failure)**: deliver failure notice to creator's origin session (different channel for cross-platform schedules)
- **L3 (on L2 failure or contract-level fault)**: admin alert via direct httpx (Telegram first, Slack `#alerts` second, disk-persisted `events` row as final)

L3 is for OPERATOR (sergey-the-admin), not user delivery.

### 7.3 Admin alert ack lifecycle

Each `admin_alert_sent` event has a UUID. Admin replies `ack <uuid>`
→ `admin_alert_acked` event with `correlates=<original_id>`.
Background monitor scans for `admin_alert_sent` events without
matching `admin_alert_acked` older than N hours; re-alerts.

---

## 8. Pause / archive lifecycle

```
active ◄────────────────────┐
  │                          │
  │ schedule_pause          │ schedule_resume
  ▼                          │
paused ─────► archived       │
  │ schedule_archive  │      │
  │                   │      │
  │                   │      │ schedule_revive (admin-gated)
  │                   ▼      │
  └────────────► active ─────┘ (only via paused after revive)
```

- `paused`: wakeup is no-op. Existing `pending` runs handled per
  `paused_pending_policy` (`let_complete` / `cancel_pending`).
  Existing `running` runs not interrupted.
- `archived`: wakeup is no-op AND existing pending runs cancelled.
  Cannot be resumed directly. `schedule_revive` transitions
  to `paused` (admin re-approves before final resume).

---

## 9. Observability primitives (over EventLedger)

- `schedule_status(id)` → recent runs (status, ts, duration_ms),
  next due_at, jobstore consistency check, pause/archive state,
  unacked alerts count.
- `schedule_diff(id, hash_a, hash_b)` → unified diff including
  template metadata + tool tag snapshots.
- `schedule_failures(limit=50)` → query EventLedger for
  `run_failed | emit_failed | source_failed | delivery_failed`
  events recent.
- `schedule_history(id)` → timeline of versions + Runs + key events.
- `schedule_health` → fire-OK rate over last 7d per schedule.
- `registry_status` → cached enums + staleness.
- `schedule_replay(id, run_id)` → re-run a past Run with stored
  inputs + stored source snapshots, in dry-run mode.

Background failure monitor: every N minutes,
`SELECT * FROM events WHERE kind='admin_alert_sent' AND id NOT IN (SELECT correlates FROM events WHERE kind='admin_alert_acked') AND ts < now - threshold`.
Re-alert.

---

## 10. Edge cases (per use case)

### 10.1 Reminders (use cases 6, 11, 13)

- User offline at fire: deliver anyway; mirror into session for next-turn pickup. Optional `defer_until_user_online`.
- Snooze: builder slot `snoozable(by_minutes=[...])`. Snooze creates a new OneOff.
- Dismiss: irreversible; ledger event so bot doesn't re-create.
- Timezone: stored UTC, rendered to user's TZ.

### 10.2 Recurring series with source + state (use cases 1, 12)

- Day-counter desync: anchor to start_date, derive `day_n`.
- Author edits source mid-run: per `live_change_policy`.
- Backfill: per `backfill_policy`.
- End-of-series: `on_complete` action.
- Source unreachable: per `fallback_policy`.

### 10.3 Conditional triggers (phase 3+)

- Polling: configurable interval; default 15min.
- Event hook: phase 3+ (needs push notifications / pubsub).
- Gate failure: treat as no-trigger; log.
- Cooldown: prevents spam on flapping conditions.

### 10.4 Multi-recipient emit

- `channels: list[Channel]`. Partial-failure: continue past failures by default; log each. `abort_on_first_fail` optional.
- Each delivery has a deterministic dedup key (idempotency).

### 10.5 YouTube watch

- Long videos: chunk at loader level.
- Auth: bot's shared YouTube API key (no per-user OAuth).
- Rate limit: respect quota.
- Output: structured JSON; emit consumes.

### 10.6 Sheet update

- Atomic per-call append.
- Row count past limit: admin alert.
- Quota exceeded: backoff + retry.
- Idempotency: per-fire key in tracking column or `sheet_dedup` gate.

### 10.7 Cross-platform (use case 14)

- `target_session_id` vs `origin_session_id` split (shipped).
- L1/L2/L3 fallback.

### 10.8 Author errors

- Typo / wrong field: ADK tool fails at call time (Pydantic).
- Hallucinated channel ID: enum-typed tool refuses.
- Verbatim paraphrasing: LiteralText / source importer prevents.
- Forgotten emit: builder requires at least one.
- LLM-picked delivery target: tool param is `Channel` enum; cannot be `StepRef`.

### 10.9 Hash drift / version mismanagement

- Body file edited post-freeze: worker detects, refuses, alerts.
- Revise without reschedule: auto-reschedule on revise.
- Parent-hash chain: revise records `parent_hash`.

### 10.10 Boot order: covered in §6.7.

### 10.11 Failure surfacing: covered in §7.

### 10.12 Replay: covered in §9.

---

## 11. Migration plan

### 11.1 Existing APScheduler jobs

Current job-store contains:
- `cron_*` / `oneoff_*` — legacy `schedule_recurring_task` / `schedule_one_off_task` jobs
- `contract:*` — v1 contracts via `contract_schedule`
- `sys_*` — admin system tasks

Migration strategy:

1. **No automatic migration.** All existing jobs continue firing
   under their current callbacks via a "compatibility worker"
   that wraps each job into a synthetic ScheduleSpec for
   ledger-recording purposes (so observability tools see them)
   but doesn't change their execution path.
2. **Opt-in migration tool**: `migrate_legacy_job_to_schedule_spec(job_id)`
   produces a draft ScheduleSpec from a legacy job's kwargs. User
   reviews via diff, dry-runs, freezes. Old `cron_*`/`oneoff_*`
   job is then `delete_scheduled_task`'d.
3. **Sunset timeline**: legacy authoring tools (`schedule_recurring_task`
   etc.) deprecated with warnings; bot's Coordinator instruction
   updated to recommend v2 templates. After ~60 days, legacy
   authoring tools removed from the ADK toolset; the
   compatibility worker remains to keep already-frozen legacy
   jobs running. After ~6 months, force-migration: legacy
   jobs are unscheduled with admin notice if not voluntarily
   migrated.

### 11.2 Existing v1 contracts

`data/contracts/*/v*.json` already on disk. Migration:

1. **Wrap into ScheduleSpec + ExecutionPlan.** Each frozen v1
   contract becomes:
   - One row in `execution_plans` table with the v1 contract's
     hash + body_json
   - One row in `schedules` table with the trigger / failure /
     enforcement extracted from the v1 contract, pointing at the
     execution_plan_hash
2. **Backfill ledger events** for prior fires (read existing
   per-fire JSONL audit files, replay into `events` table).
   Bounded to last N fires to avoid massive backfill.
3. **`apscheduler_jobs` table updated** to call the new wakeup
   function (instead of `run_contract_fire`) for all `contract:*`
   jobs. Wakeup function detects the migrated ScheduleSpec by
   `schedule_id`, follows the new Run/EventLedger path.
4. **No forced re-author**: v1 contracts keep firing under their
   ExecutionPlan body unchanged. New revisions go through v2
   authoring (templates / typed tools).

Migration is a one-time script run at the deploy boundary. The
script writes a migration manifest event to the ledger
(`migration_v1_to_v2_complete`) so the system can refuse to
re-run.

### 11.3 Test suite migration

V1 test files under `tests/` are scoped to the v1 architecture:

```
tests/test_contracts_schema.py
tests/test_contracts_store.py
tests/test_contracts_emit.py
tests/test_contracts_loaders.py
tests/test_contracts_worker.py
tests/test_contracts_executor.py
tests/test_contracts_rigor.py
tests/test_contracts_admin_alert.py
tests/test_contracts_audit_mirror.py
tests/test_contracts_emit_status_check.py
tests/test_scheduling_*.py
```

All assert against v1's Contract model, `_coerce_spec` flow,
JSONL audit files, `data/contract_failures.jsonl` shape, and
APScheduler-as-state-truth assumptions. None of those hold in
v2.

**Replacement strategy**: rewrite, not retrofit. The new test
suite is built test-first as each implementation phase lands.

Phase-1 test files (rough layout):

```
tests/test_schedules_schema.py            # ScheduleSpec, ExecutionPlan, Run, Event Pydantic models
tests/test_schedules_storage.py           # SQLite CRUD + transactions
tests/test_schedules_lifecycle.py         # state-machine transitions, transition table coverage
tests/test_schedules_claim.py             # single-flight, race conditions, recovery
tests/test_schedules_idempotency.py       # emit dedup across retries (root_run_id stability)
tests/test_schedules_invariants.py        # one test per phase-1 invariant in §4.0.4
tests/test_schedules_wakeup.py            # APScheduler wakeup callback → Run+events insertion
tests/test_schedules_registry.py          # cached enums + provenance + no-cache path
tests/test_schedules_validation.py        # validate_schedule_spec chokepoint
tests/test_schedules_dryrun.py            # three modes + as_of_datetime + freeze handshake
tests/test_schedules_authoring_tools.py   # typed ADK tools, builder produces valid ScheduleSpec
tests/test_schedules_templates.py         # OneOffReminder, RecurringSeriesFromSource, ChannelDigest
tests/test_schedules_sources.py           # source loader types, snapshots, fallback policies
tests/test_schedules_reasoning_readonly.py # read-only tool-mode enforcement
tests/test_schedules_state_cas.py         # cross-fire state writes with CAS
tests/test_schedules_pause_archive.py     # pause/archive/revive lifecycle
tests/test_schedules_failure_policy.py    # on_failure routing, retry chain, admin alert path
tests/test_schedules_observability.py     # status, diff, replay, failure monitor over ledger
tests/test_schedules_migration.py         # legacy job + v1 contract import
tests/test_schedules_boot_recovery.py     # crash/restart scenarios from §4.0.5
tests/test_schedules_emit_signature.py    # adapter signature conformance + tool tags
```

**V1 test deprecation**: keep v1 tests passing until the legacy
authoring path is removed (the ~60-day sunset window in §11.1).
Run them in CI alongside v2 tests during that period — they
exercise the compatibility worker. After the sunset, delete
v1 test files in one commit alongside the legacy tool removal.

**Test coverage targets**:
- Every state in the lifecycle state machine has at least one
  test exercising each transition into and out of it.
- Every invariant in §4.0.4 has at least one test that would
  fail if the invariant were violated (negative-case test).
- Every event kind in the canonical list (§4.0) has at least
  one test that writes it AND one observability test that reads
  it.
- Every edge case in §10 has at least one test.
- Every Decision Log entry (D1–D7) has at least one test
  pinning the chosen behavior (so a future contributor who
  considers reverting one sees a red test, not just a comment).

**Test-first invariant**: each implementation-order step (§12)
ships with its test file in the same commit. CI blocks merges
where invariant or transition coverage decreases.

### 11.4 Agent guidance / skill migration

V1's authoring path lives in agent-facing instructions and skill
docs as much as it lives in code. When v2 lands, the following
files MUST be updated in the same change-set as the code that
deprecates each v1 surface. Missing one of these silently leaves
the bot pointed at the old API.

**Files to update:**

- `app/sub_agents/coordinator_agent.py` — routing instruction
  text must switch from contract-pipeline vocabulary to schedule
  templates / typed tools. Replace:
  - "Use `contract_draft_validate` → `contract_dry_run` →
    `contract_freeze` → `contract_schedule` for recurring tasks"
  - "Use `contract_inspect` / `contract_revise` / …"
  with:
  - "For recurring/structured work: pick a template
    (`OneOffReminder`, `RecurringSeriesFromSource`,
    `ChannelDigest`) and fill its slots."
  - "For ad-hoc workflows that no template covers: typed-tool
    `schedule_draft_start` → `schedule_dry_run` →
    `schedule_freeze`."
  - "Never compose freeform JSON or write Python builder code;
    those paths are removed."
- `AGENTS.md` / `docs/AI_EDITS.md` — add a "scheduling law":
  > **Scheduling law**: every scheduled work item is created via
  > a v2 typed tool. Templates first; CustomFlow only when no
  > template fits AND admin approval triggers are accepted. No
  > freeform JSON dict, no `contract_freeze(spec: dict)`, no
  > `schedule_recurring_task` for new recurring work.
- `docs/CONTRACTS.md` — the v1 reference. After v2 lands, split
  into:
  - `docs/CONTRACTS.md` → preserved as "Legacy v1 contracts —
    compatibility worker only. New work goes through v2."
  - `docs/SCHEDULES.md` → v2 user-facing reference; promotes the
    template surface; documents the typed-tool registry.
- `docs/CONTRACTS_V2_DESIGN.md` (this doc) — stamped "implemented
  through phase N" as steps land; preserved as the historical
  design record.
- `docs/INDEX.md` and `docs/TOOLS.md` and `docs/TOOLSETS.md` —
  regenerated via `scripts/gen_docs.py` after every tool addition
  or rename in the v2 build-out.
- `docs/AGENTS_INVENTORY.md` — regenerated alongside INDEX.
- `skills/` (or wherever local skill files live in the repo /
  `.agents/skills/`) — any skill that references contract
  authoring vocabulary needs an update. New skill file
  `skills/schedule-authoring/SKILL.md` covers v2 patterns:
  template selection by intent, slot-filling conversation
  workflow, dry-run reading, freeze + status check.
- `docs/ROUTING.md` — sub-agent routing pattern for scheduling
  requests; should point at the Coordinator's
  schedule-template tools, not at the deprecated contract path.
- Tool-name surface — the OLD agent-facing tools
  (`contract_freeze`, `contract_dry_run`, `contract_revise`,
  `contract_schedule`, `contract_unschedule`,
  `schedule_recurring_task`, `schedule_one_off_task`,
  `delete_scheduled_task`, `edit_scheduled_task`) are renamed
  / replaced:
  - `contract_*` (old) → split: durable workflow body authoring
    handled internally; user-facing surface becomes
    `schedule_*` (new).
  - `schedule_recurring_task` / `schedule_one_off_task` (legacy)
    → deprecated; replaced by template tools
    (`schedule_create_reminder`, etc.) that internally produce
    ScheduleSpec.
- Coordinator examples / few-shot prompts — update with v2
  template invocations. The Coordinator must STOP showing v1
  examples to itself.

**Guardrail test (mandatory)**: an integration-style test that
parses the Coordinator instruction text and asserts:

- Does NOT contain the string `contract_freeze(spec` or
  `contract_freeze(spec:` (signature of the deprecated freeform
  path).
- Does NOT contain `schedule_recurring_task(` or
  `schedule_one_off_task(` after v2's matching tools land.
- DOES contain references to the v2 template tool names
  (`OneOffReminder`, `RecurringSeriesFromSource`,
  `ChannelDigest`).
- DOES contain the scheduling-law clause.

Failure of this test in CI is a HARD BLOCK on merging changes
that would leave the bot's authoring instructions out of sync
with the implemented surface.

**Rollout coordination**: the v2 phase plan (§12) ships tools
incrementally. The Coordinator instruction MUST stay consistent
with what's actually deployed. Each phase commit that adds a
new schedule_* tool also updates Coordinator instruction +
regenerates docs/INDEX.md + adds / updates the guardrail test
assertions in the SAME commit.

---

## 12. Implementation order

Each step is independently shippable + testable. Tag at each step
so revert is a single git command.

1. **Schema-only design**: ScheduleSpec, ExecutionPlan, Run, EventLedger, schedule_state, source_snapshots Pydantic models + SQLite DDL + state-transition unit tests + invariants spec'd as documented constraints. No execution code.
2. **Adapter / source / loader input-output Pydantic models + tool metadata tags**: per-adapter request/response shapes. No runtime use yet.
3. **Storage layer**: SQLite migrations, WAL mode, basic CRUD helpers + tests.
4. **APScheduler-as-wakeup-only + Run claim**: wakeup callback that inserts pending Runs in TX with `run_created` events. Worker pool loop that claims runs. Boot recovery scan. State-machine transitions implemented + tested with no execution body (Runs go pending → claimed → running → succeeded with empty execution).
5. **APScheduler binding + boot sequence** (renumber 2026-05-15, deferred out of step 4 per Sergey's phase-4 closeout call): `SchedulerBinding` wrapping `AsyncIOScheduler` against `SQLAlchemyJobStore` + `misfire_grace_time` per §4.0.5; `boot_runtime` runs recovery → binding start → OneOff backfill → register active schedules → start worker pool, in that order; `lifecycle.py` exposes pause / archive / resume / revise hooks the future authoring path will call. Still NO production cutover — `run_bot.py` untouched; v1 scheduler stays the production wakeup source until step 9 (OneOffReminder end-to-end).
6. **Registry cache for channels/sheets/docs**: cache + lazy refresh + no-cache-and-network-down handling.
7. **Typed ADK tools**: build ScheduleSpec drafts via tool calls. CustomFlow path lands here. Each tool calls `validate_schedule_spec` after its mutation.
8. **Dry-run handshake + boot self-test**: three modes + `as_of_datetime` + freeze records snapshot + boot self-test gating.
9. **OneOff trigger + `OneOffReminder` template + emit-only path**: first end-to-end working schedule. Reminders fire via the new path, no ExecutionPlan, no source.
10. **Source loaders + snapshot infrastructure**: `source_literal`, `source_drive_file`, `source_local_file`, `source_slack_thread`. Per-source cache + fallback + drift + retention + on_oversize.
11. **Source-driven fire-path cutover + `RecurringSeriesFromSource`** (split 2026-05-16 into 11A/11B per `docs/PHASE_11_PLAN.md` §0 — reviewer-approved refinement):
    - **11A (shipped — phase 11)**: the step-10 source layer (loaders / snapshot / cache / resolver), built unwired at step 10, goes LIVE in the worker fire path. A source-driven `ScheduleSpec` (`execution_plan_hash` → `ExecutionPlan` with `InputSpec.source_ref`, zero reasoning) fires end to end: claim → `resolve_source` per source-bearing input on the claimed connection → emit. Ships the `RecurringSeriesFromSource` template with STATELESS strategies only (`whole` / `skip_unchanged`), authored through the SHARED spine EXTENDED additively (§0.3, reviewer-ratified, scoped to the authoring spine): the freeze/commit trigger gate admits `{one_off, cron}` (others → `trigger_type_pending_step_unlock`); `commit` persists `insert_execution_plan`+`insert_schedule` atomically in ONE transaction (both-or-neither). `skip_unchanged` reads the additive `ResolveOutcome.changed_vs_prior` signal (§5.3.5 — the worker NEVER re-reads the snapshot table, Q5); the no-op success rides the EXISTING `running → succeeded` `RUN_SUCCEEDED` write via the typed additive `RunSucceededPayload.skipped_unchanged` discriminator (Option B, §0.2 — no new `EventKind`, no v002 events-schema migration, no second transaction). The `OneOffReminder` emit path + the emit/cache/resolver modules stay LITERALLY byte-untouched (additive cutover, §11.1; v1 scheduler untouched).
    - **11B (deferred)**: `ChannelDigest` (needs the LLM reasoning-chain executor + `summary_prompt`) and stateful `RecurringSeriesFromSource` progress (needs step-13 cross-fire `schedule_state`). **Phase-1 completion lands with 11B.**
12. **Read-only reasoning + emit-only writes enforcement**: tool-metadata-driven runtime block.
13. **Cross-fire state with locks + CAS**: state_read / state_write primitives backed by `schedule_state` table + CAS.
14. **Idempotency + cancellation**: per-emit idempotency keys; paused/archived enforcement; `paused_pending_policy`.
15. **Observability**: `schedule_status`, `schedule_diff`, `schedule_replay`, background failure monitor over EventLedger.
16. **Migration tooling**: v1 contract → v2 wrap; legacy job → ScheduleSpec import; ledger backfill.
17. **Phase 2+**: interval / conditional / branch / loop / parallel triggers and steps.

Dependencies (so order isn't arbitrary):
- Step 4 depends on 1, 3.
- Step 5 depends on 4 (binding wraps phase-4 runtime).
- Step 7 depends on 1, 2, 3, 6.
- Step 8 depends on 4, 7.
- Step 9 depends on 4, 5, 7, 8 (first cutover; needs binding + authoring + dry-run all live).
- Step 10 depends on 1, 2, 6.
- Step 11 depends on 10 + 9 (templates need both source infra and the ScheduleSpec authoring loop). 11A shipped phase 11; 11B deferred (see step 11).
- Step 12 depends on 2.
- Step 13 depends on 3.
- Step 14 depends on 4, 12, 13.
- Step 15 depends on most of the above.
- Step 16 can land in parallel with later phases.

### 12.1 Phase commit discipline (mandatory)

Three implementation-process invariants captured in this doc so
they can't drift between design and execution:

1. **Phase commits stay small.** Each phase in §12 is one logical
   change that ships in ONE commit (or one small commit series
   with explicit dependencies). Mixing two phases into one
   commit defeats the per-step revert tag strategy and obscures
   review. CI hard-blocks a commit that touches files belonging
   to two or more distinct phases unless the commit message
   explicitly justifies the merge AND a reviewer approves.

2. **Phase 1 ≠ runtime behavior.** Phase 1 is schemas + DDL +
   migration skeleton + lifecycle/state-transition tests +
   invariant tests, ONLY. No worker execution code. No new
   APScheduler callbacks wired. No agent-facing tools registered.
   The runtime path lights up at step 4 (wakeup callback +
   worker pool, empty body), gains production wiring at step 5
   (binding + boot sequence, still test-rig-only), and the
   first actual emit fires at step 9 (`OneOffReminder` template
   end-to-end). Anything that produces side effects in
   production before step 9 is out-of-scope for the merge that
   introduces it.

3. **Old scheduler stays untouched until v2 fires end-to-end in
   tests.** No edits to `app/contracts/*`, `app/tasks.py`,
   `app/contracts/executor.py`, `app/scheduler_instance.py`, or
   any v1 contract under `data/contracts/*` until the new Run
   path can fire a `OneOffReminder` template end-to-end in
   tests (step 9 completion). The compatibility worker (§11.1)
   only LANDS at that point. Touching v1 before then risks
   destabilising production-running schedules
   (`linux_mastery_30_days_v2`, `ai_pilot_*`,
   `fba_listing_analysis_b098pc693h`) without the new path
   ready as a fallback.

These three invariants are CI-enforceable:
- Invariant 1: commit-touches-only-files-in-one-phase check (rough
  heuristic; manual override allowed with explicit justification).
- Invariant 2: phase-1 PR cannot import from a "runtime" module
  set (defined in `pyproject.toml` per-phase configuration).
- Invariant 3: phase-1-through-8 PRs cannot edit files under
  `app/contracts/`, `app/tasks.py`, `app/contracts/executor.py`,
  `app/scheduler_instance.py`, `data/contracts/` (boundary
  shifted from 1-through-7 with the 2026-05-15 §12 renumber;
  cutover moved from step 8 to step 9).

Violation = CI red. Lifted only at the phase boundary where the
invariant is no longer load-bearing.

---

## 13. Decision Log

For each major architectural choice, record: decision, why,
rejected alternative, risk. Updated as decisions are made or
revised.

### D1. ScheduleSpec is the root object, not Contract

**Decision**: ScheduleSpec is the persisted root. ExecutionPlan
(the artifact formerly called Contract) is referenced by
ScheduleSpecs that need multi-step hash-pinned workflows. Simple
reminders are ScheduleSpecs with no ExecutionPlan.

**Why**: V1 conflated 7 concerns into one Contract object
(trigger + ownership + plan + delivery + failure + audit + status).
Each concern has a natural scope; mixing them produces fights
between schedule-level intent ("pause this") and plan-level
invariants ("hash-pin the body"). Splitting cleanly lets
lightweight cases (reminders, follow-ups) avoid the heavy
contract ceremony while heavy cases (FBA audit, recurring series)
keep their immutable execution body.

**Rejected alternative**: keep Contract as root and just add a
`light_mode` flag for reminders. Rejected because a flag-based
split inside one type still drags reminder cases through the
ExecutionPlan validators / freeze handshake / emit pipeline they
don't need, and obscures the architecture for readers.

**Risk**: more indirection. A new contributor must internalize
two related-but-distinct types (ScheduleSpec, ExecutionPlan).
Mitigation: documentation + naming (user-facing "scheduled task";
engineering keeps "contract" for ExecutionPlan).

### D2. APScheduler is wakeup-only, not state truth

**Decision**: APScheduler's only job is to invoke a wakeup
callback at the right time. The callback computes due times,
inserts Run rows + `run_created` events into SQLite in a single
transaction, and returns. Worker pool (independent of
APScheduler) claims pending Runs via `UPDATE … WHERE status='pending' RETURNING`.

**Why**: V1 treated APScheduler as both the scheduler AND the
execution-state holder. That meant retries, replays, paused/
archived states, and backfill all required hacky interactions
with APScheduler internals. Mature schedulers (Sidekiq, Celery,
RQ, Temporal) separate "wake me up" from "what to do now"
because the two concerns evolve at different speeds.

**Rejected alternative**: use APScheduler more heavily (custom
trigger types, custom job stores, manipulating `next_run_time`).
Rejected because it locks the design into APScheduler's
abstractions and makes the durable-Run model harder to express
cleanly.

**Risk**: more components to operate (wakeup callback +
worker pool + SQLite tables). Mitigation: SQLite is already in
use; the worker pool is a small async task. The boundaries are
crisp.

### D3. SQLite as the single state store

**Decision**: All v2 state (`schedules`, `execution_plans`,
`runs`, `events`, `schedule_state`, `source_snapshots`) lives in
the existing `data/ori-scheduler.db` SQLite file alongside
APScheduler's `apscheduler_jobs` table. WAL mode for concurrent
readers + single writer. Large source-snapshot bodies live on
disk, referenced by hash in SQLite.

**Why**: SQLite is already deployed and durable. JSONL/per-file
state forces fragmented audit, manual joins, and ad-hoc query
tooling. A relational store gives transactional state machine
invariants for free (transition + event written atomically).
SQLite WAL handles concurrent readers (observability tools)
without blocking the writer (worker pool).

**Rejected alternative**: continue with JSONL audit files +
`failures.jsonl` + filesystem state. Rejected because v1 already
proved this fragments the audit story: today's failure log,
fire audit, source snapshot, and jobstore are four separate
places, each parsed differently. The unified ledger needs one
queryable surface.

**Rejected alternative**: Postgres. Rejected because the bot is
a single-process daemon; SQLite avoids a network hop and an
extra operational surface. If scale demands it later, the SQL
schema is portable.

**Risk**: SQLite single-writer means high-throughput contract
fires queue at the writer. Mitigation: per-event writes are
small; expected fire rate is dozens-per-minute peak. Adding a
second SQLite DB (e.g. one for events vs. one for runs) is a
straightforward escape hatch if write contention shows up.

### D4. Compatibility migration, not forced

**Decision**: Existing APScheduler jobs (`cron_*`, `oneoff_*`,
`contract:*`) continue firing under their current callbacks via
a compatibility wrapper that records their fires into the v2
ledger for observability. New v1 contract freezes still work
during the deprecation window. Migration to ScheduleSpec is
opt-in per contract / per legacy job. Sunset legacy authoring
tools after ~60 days; force-migrate after ~6 months.

**Why**: A clean break would unschedule every existing job at
deploy time. Sergey has live production contracts (linux_mastery,
fba_listing_analysis, ai_pilot_*) running daily; forced-migration
risks downtime. Opt-in keeps continuity while v2 absorbs the
authoring surface.

**Rejected alternative**: forced migration at deploy time
(unschedule all, require admin to re-author each). Rejected
because the deploy boundary becomes a manual-toil event.

**Risk**: dual-code-path period (compatibility worker + v2
worker) increases maintenance surface. Mitigation: sunset
timeline is explicit; compatibility worker is small (just
records ledger events; doesn't change v1 execution).

### D5. Simple reminders stay lightweight, NOT contracts

**Decision**: ScheduleSpec with no `execution_plan_hash` is a
first-class shape for one-off reminders, follow-ups, and any
"deliver this text at time T to user X" use case. They go
through the same wakeup → Run → ledger pipeline as
ExecutionPlan-backed schedules. Reminders skip only the
LLM-side ceremony:

- No LLM dry-run (no reasoning to render)
- No emit-args render against state (text is literal)
- No source resolution (no inputs)
- No loader execution

Reminders STILL require the rest of the authoring ceremony
(review round 6 correction — earlier draft skipped too much):

- **Target validation**: channel/recipient must exist in the
  registry enum (Channel/User), same allowlist that
  ExecutionPlan emits use.
- **Permission check**: owner must hold the scope to deliver to
  the target (e.g. has Telegram chat record with the recipient,
  has Slack post-message permission for the channel).
- **Timezone parse**: `at_iso_datetime` must parse cleanly into
  UTC; `timezone` must be a known IANA name.
- **Preview**: bot shows the rendered delivery preview ("at
  2026-05-15 14:00 Europe/Kyiv I will send `<text>` to `<target>`")
  and requires user confirmation before freeze.
- **Freeze + persist**: row written to `schedules` table, wakeup
  job registered with APScheduler, `schedule_created` event
  written to the ledger.

These checks are mechanical and fast — they don't cost an LLM
call or a network fetch.

**Why**: V1's mandatory contract ceremony for "ping me in 2h"
was friction without value because the LLM/source/emit-render
parts had nothing to chew on. Reminders skip THAT layer.
Skipping target validation / permissions / TZ parse / preview
would reintroduce the same author-error class
(`sl_<id>` as a channel, etc.) the v2 redesign exists to
prevent.

**Rejected alternative**: keep reminders out of v2 entirely;
preserve legacy `schedule_one_off_task` indefinitely. Rejected
because it perpetuates the dual-system problem the rewrite is
meant to solve. ScheduleSpec without ExecutionPlan is the
right home for them.

**Rejected alternative**: every ScheduleSpec has a degenerate
ExecutionPlan with no reasoning, just emits. Rejected because
the type system would lie ("Plan exists" when really the work
is in the emit) and the freeze ceremony would still apply.

**Risk**: two ScheduleSpec shapes (plan-backed and reminder-only)
in one type. Mitigation: Pydantic discriminator on `execution_plan_hash` is None vs not-None; downstream code matches once.

### D6. Read-only reasoning by default

**Decision**: `ReasoningStep.tool_mode` defaults to `read_only`,
which blocks at the worker layer any tool tagged with
`write_external`, `send_message`, `filesystem_write`, or
`privileged`. To allow a write tool, the author MUST explicitly
set `tool_mode=write_allowed` (which then triggers admin
approval in CustomFlow).

**Why**: V1 allowed bezos to put "Data Analyst agent records to
sheet" inside step_6's prompt. The LLM then called a write tool
mid-reasoning. Non-deterministic, no replay, no idempotency. The
emit adapter chain exists specifically to make writes
deterministic; bypassing it defeats the design.

**Rejected alternative**: `mutates: bool` flag on tools.
Rejected because it can't distinguish (e.g.) "sends a Slack
message" from "writes to local disk" — and the policies for
each in different contexts are different. Multi-tag set
composes precisely.

**Risk**: every existing tool needs tag audit. Default-on
(`write_external` for unknowns) is fail-safe but may block
legitimate reads until tags are corrected. Mitigation: incremental
tagging pass before the step-12 runtime enforcement
lands. (Step 11 — the source-driven cutover — shipped in
phase 11 emit-only with NO reasoning-chain executor, so
the write-tool-mid-reasoning risk this guard addresses
does not yet apply; the tag-audit prerequisite carries
forward to step 12.)

### D7. Event ledger is the audit source of truth

**Decision**: All state transitions write a row to the `events`
table. Observability tools query the table. Failure monitor
queries the table. Replay tool reads the table.

**Why**: V1 fragmented audit across per-fire JSONL audit files,
`failures.jsonl`, per-fire source snapshots, and jobstore
state. Cross-cutting queries ("show me all failed runs across
all schedules in the last 24h") required parsing multiple
formats. A single ledger collapses that to one query.

**Rejected alternative**: keep JSONL per-fire + add a separate
SQLite events table that summarises. Rejected because two
sources of truth always drift; one wins, the other rots.

**Risk**: ledger volume grows. Mitigation: retention policy
prunes old events (configurable per schedule). Important
historical events (`schedule_archived`, `migration_*`) flagged
as non-prunable.

---

## 14. Open questions

Things still requiring confirmation or decision before phase 1
implementation:

1. **Worker pool size**: how many concurrent workers? Bound by
   SQLite single-writer throughput + per-fire CPU. Sketch
   benchmarks before fixing the default.
2. **Retry backoff defaults**: exponential? Linear? Specific
   per `on_failure.action`? Need explicit policy.
3. **Snapshot retention disk budget**: 30 snapshots × N
   schedules × M sources each — projection + alert threshold
   needed.
4. **EventLedger retention defaults**: prune after how long?
   Per-event-kind retention? Permanently keep `schedule_archived`
   and migration events?
5. **Conditional trigger polling cadence**: 50 conditional
   schedules at 5min = 600 polls/hour. Coalescing layer or
   accept the load?
6. **Cross-schedule state sharing**: D5 leaves `schedule_state`
   per-schedule. Do we need a shared state namespace later
   (e.g. "all AI Pilot schedules share a 'last_posted_topic'
   key")? Defer or design now?
7. **Template versioning across boundary**: when
   `RecurringSeriesFromSource@v2` adds a slot, existing
   `@v1`-frozen schedules — keep loadable forever, or force
   re-author?
8. **Admin approval flow specifics**: today's "Approve ACT-..."
   pattern reused for CustomFlow approvals? Or new mechanism?
9. **Migration ledger backfill window**: how many prior fires
   per legacy contract get replayed into events?
10. **Multi-process safety**: bot is single-process today. If
    we ever fork (multiple supervisor children), the worker
    pool's single-flight assumption needs reconsideration.

---

## 15. Vocab for the reviewer

If you flag any of the following, that's useful signal:

- Use cases I haven't enumerated that this design can't serve.
- Edge cases I missed.
- Schema fields that look right today but constrain us tomorrow.
- Invariants stated above that AREN'T mechanically enforced.
- Cheaper paths to the same correctness outcome.
- Names or vocabulary that will confuse users / future engineers.

Goal of v2: **the wrong thing is impossible to construct, not
caught at validation**. Validators stay as the safety net.
Architecture, schemas, and lifecycle do the heavy lifting.
