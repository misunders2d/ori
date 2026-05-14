# Scheduler v2 — Phase 4 plan

Phase 3 shipped 2026-05-14 (tag `v2-phase-3-complete`). Phase 4
implements design contract §12 step 4:

> APScheduler-as-wakeup-only + Run claim. Wakeup callback that
> inserts pending Runs in TX with `run_created` events. Worker
> pool loop that claims runs. Boot recovery scan. State-machine
> transitions implemented + tested with no execution body
> (Runs go pending → claimed → running → succeeded with empty
> execution).

This is the first phase where runtime behavior actually moves.
The data plane shipped in phase 3 stays the building blocks;
phase 4 wires them into a loop. **No reasoning, no emit, no
external I/O** — the worker body is intentionally empty in
phase 4. Real execution lands in phases 9-11.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §4.0.1 (lifecycle + state
  transitions), §4.0.4 (invariants the worker must encode),
  §6.1 (worker claim SQL).
- `docs/PHASE_3_PLAN.md` — the storage primitives this phase
  consumes.

---

## 1. Scope statement

### In scope (phase 4)

1. **State machine** — a pure module describing the legal
   `RunStatus` transitions. Used by the worker to validate each
   transition before issuing the storage update. Phase 3 left
   the storage layer policy-free; phase 4 introduces policy.
2. **Claim primitive** — `claim_run(conn, run_id, *, claimed_by,
   now) → bool`. Wraps the design §6.1 single-flight UPDATE.
   This is the helper phase 3 deliberately deferred.
3. **Recovery scan** — boot-time helper that finds stale
   claimed / running rows past a timeout and routes each by
   `RecoveryPolicy`.
4. **Worker loop** — long-running async task that polls
   `list_pending_due`, claims a run, transitions
   `claimed → running → succeeded` (empty body), recording
   matching events for every transition. The body itself is
   `await asyncio.sleep(0)` or equivalent no-op — real
   reasoning + emit lands in later phases.
5. **Wakeup callback** — `wakeup(conn, schedule_id, *, now) →
   list[str]`. Reads the ScheduleSpec, computes any due
   `due_at`(s), INSERTs Run rows + matching `run_created`
   events in the same TX. Returns the inserted run ids.
   Callable directly (testable in isolation).
6. **APScheduler binding (callable surface only)** — a thin
   module that exposes the wakeup function as the callback
   APScheduler will register. Real APScheduler scheduler
   construction + start-stop is NOT done in phase 4 — the
   wakeup function is invokable from tests via direct call.
   Phase 5 (or whichever introduces production wiring) takes
   the next step. Documented in §6 below.

### Out of scope (phase 4)

- **Any reasoning step execution.** Worker body is empty; tests
  pin that the body is a literal no-op.
- **Any emit adapter dispatch.** The runtime ingest of phase-2
  EmitDescriptor lands in phase 11 alongside read-only-reasoning
  enforcement.
- **Real APScheduler scheduler construction** — wakeup is a
  callable; the loop that calls it on a cron schedule comes
  later. Tests exercise wakeup directly with synthetic clocks.
- **Source loaders** (phase 9).
- **Cross-fire state CAS calls from inside the worker body** —
  the helper landed in phase 3 but the policy of when the worker
  invokes it ships with the real execution path.
- **Authoring tools / freeze tool / dry-run handshake.**
- **Coordinator / sub-agent edits.**
- **V1 scheduler edits** (`app/contracts/`, `app/tasks.py`,
  `app/contracts/executor.py`, `app/scheduler_instance.py`,
  `data/contracts/`). Phase guard still enforces this through
  phase 7.
- **Implicit production-DB access.** Every helper continues to
  take an explicit connection. The worker can take a connection
  factory at construction time for testability.

---

## 2. New file paths

```
app/v2/
  runtime/
    __init__.py                # re-exports
    state_machine.py           # legal RunStatus transitions
    claim.py                   # single-flight claim_run helper
    recovery.py                # boot-time stale-run scan
    worker.py                  # async worker loop (empty body)
    wakeup.py                  # wakeup callback function

tests/v2/
  test_runtime_state_machine.py
  test_runtime_claim.py
  test_runtime_recovery.py
  test_runtime_worker.py
  test_runtime_wakeup.py

docs/PHASE_4_PLAN.md           # this file
```

Existing files touched (scaffolding only):

- `scripts/check_phase_scope.py` — adds `PHASE_ALLOWLIST[4]`
  mirroring phase 3's machinery surface with
  `docs/PHASE_4_PLAN.md` replacing `docs/PHASE_3_PLAN.md`.
- `.v2-current-phase` — bumped from `3` to `4`.

Everything else is additive. No edits to `app/v2/storage/*` —
those primitives stay frozen; the runtime layer composes them.

---

## 3. State machine

### 3.1 Legal transitions

```
pending   → claimed     (worker wins single-flight claim)
pending   → cancelled   (schedule paused/archived enforcement)
claimed   → running     (worker has started the body)
claimed   → pending     (recovery: clear stale claim)
claimed   → failed      (recovery: mark abandoned claim)
running   → succeeded   (worker body completed)
running   → failed      (worker body raised; per FailurePolicy)
running   → pending     (recovery: re-queue stale running run)
running   → failed      (recovery: mark abandoned running)
```

Terminal states: `succeeded`, `failed`, `cancelled`. Retries
land as NEW pending Run rows with `parent_run_id` set, NOT by
re-statusing a failed row (round-6 invariant; storage layer's
RunStatus CHECK enforces no `retry_pending`).

### 3.2 Module API

```python
# app/v2/runtime/state_machine.py

class IllegalTransitionError(ValueError):
    """Raised when a caller attempts a status change that
    isn't in the legal-transitions table."""

LEGAL_TRANSITIONS: frozenset[tuple[RunStatus, RunStatus]] = frozenset({
    (RunStatus.PENDING,   RunStatus.CLAIMED),
    (RunStatus.PENDING,   RunStatus.CANCELLED),
    (RunStatus.CLAIMED,   RunStatus.RUNNING),
    (RunStatus.CLAIMED,   RunStatus.PENDING),
    (RunStatus.CLAIMED,   RunStatus.FAILED),
    (RunStatus.RUNNING,   RunStatus.SUCCEEDED),
    (RunStatus.RUNNING,   RunStatus.FAILED),
    (RunStatus.RUNNING,   RunStatus.PENDING),
})

def is_legal_transition(src: RunStatus, dst: RunStatus) -> bool:
    return (src, dst) in LEGAL_TRANSITIONS

def assert_legal_transition(src: RunStatus, dst: RunStatus) -> None:
    """Raise IllegalTransitionError when (src, dst) isn't legal.
    Worker + recovery scan use this as the policy chokepoint
    before issuing any storage update."""
```

Pure data — no I/O. Worker imports and calls before any
`mark_run_status` / `claim_run` / paired transition call. If the
storage layer would accept the unpredicated UPDATE anyway
(phase 3 design), the state machine refuses first.

### 3.3 Tests

- Every entry in `LEGAL_TRANSITIONS` returns True.
- Each terminal status → any other → False (parametrised).
- Every illegal combination explicitly tested via `assert_legal_transition` raising `IllegalTransitionError`.
- Pin against silently dropping a legal transition: assert
  `LEGAL_TRANSITIONS` is non-empty + includes the canonical
  paths.

---

## 4. Claim primitive

### 4.1 Module API

```python
# app/v2/runtime/claim.py

class IllegalClaimError(ValueError):
    """Raised when claim_run preconditions are violated
    (naive now, etc.)."""

def claim_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    claimed_by: str,
    now: datetime,
) -> bool:
    """Attempt to atomically claim a pending run.

    Returns True iff this caller owns the run after the call;
    False iff another worker won the race OR the run is no
    longer pending OR a concurrent run on the same schedule
    is already claimed/running (single-flight).

    Implementation runs the design §6.1 UPDATE:

        UPDATE runs SET
            status = 'claimed',
            claimed_by = ?,
            claimed_at = ?
        WHERE id = ?
          AND status = 'pending'
          AND NOT EXISTS (
            SELECT 1 FROM runs r2
            WHERE r2.schedule_id = runs.schedule_id
              AND r2.status IN ('claimed', 'running')
          )

    SQLite WAL serialises writers, so the single-flight
    predicate evaluates atomically with the UPDATE. The helper
    does NOT append a paired event row — the worker that wins
    the claim writes the `run_claimed` event in a follow-up
    storage call (or via update_run_status_and_append_event for
    the next transition). See §3.4 for the event-emission rule.

    On successful claim, the helper also writes the
    ``run_claimed`` Event in the same transaction (uses
    transactions.transaction(conn) + append_event from phase 3).
    Atomicity-on-failure: if the event INSERT fails the claim
    UPDATE rolls back.
    """
```

### 4.2 Why no UPDATE+INSERT helper for claim

The phase 3 `update_run_status_and_append_event` helper assumes
an unpredicated UPDATE-by-PK. Claim's single-flight predicate
makes it a different shape: the UPDATE may match 0 rows even
when the row exists (because another worker won). The claim
helper inlines the WHERE-clause + the matching event INSERT
inside one `transaction(conn)` block.

### 4.3 Tests

- Single pending run → claim_run returns True; status flips
  to `claimed`; `claimed_by` + `claimed_at` set; `run_claimed`
  event appended.
- Single claimed-then-stale → claim_run returns False (status
  guard fires).
- Two pending runs on different schedules → both claim
  independently.
- Two pending runs on the SAME schedule → only one claims
  (single-flight predicate fires). The loser observes False
  and the run stays pending.
- A pending run on schedule A + a claimed run on the SAME
  schedule → False (single-flight).
- A pending run on schedule A + a claimed run on a different
  schedule → True (per-schedule predicate, not global).
- Naive `now` rejected.
- Atomicity-on-failure: if the event INSERT fails (pre-seeded
  duplicate event id), the claim UPDATE rolls back; status
  stays `pending`.

---

## 5. Recovery scan

### 5.1 Module API

```python
# app/v2/runtime/recovery.py

@dataclass(frozen=True)
class RecoveredRun:
    """One row from the recovery scan."""
    run_id: str
    schedule_id: str
    prior_status: RunStatus   # claimed OR running
    applied_policy: RecoveryPolicy

def scan_stale_runs(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    timeout: timedelta,
) -> list[RecoveredRun]:
    """Boot-time scan + remediation.

    SELECTs runs in status claimed/running where
    ``claimed_at < (now - timeout)``. For each, the policy is
    chosen by prior status:

      - prior = claimed  → RecoveryPolicy.MARK_FAILED
        (worker died before starting; refuse to re-execute).
      - prior = running  → RecoveryPolicy.QUEUE_RETRY
        (worker died mid-execution; insert a fresh pending Run
        with parent_run_id = the stale run, attempt += 1,
        root_run_id carried forward).

    The third RecoveryPolicy value (CLEAR_CLAIM) is reserved
    for a manual admin tool that flips a stuck claimed row
    back to pending without bumping attempt. Phase 4 does NOT
    expose that path — adding it later is a separate slice.

    Each remediation is an atomic transaction: status change +
    matching event (kind=run_recovered) + optional new run row.
    Failure of any one remediation logs + continues with the
    rest; the scan returns the list of recovered rows.
    """
```

### 5.2 RecoveryPolicy enum naming drift (open)

Design §4.0.4 row 5 references `RETRY_PENDING` / `MARK_FAILED`
/ `RESUME`. The phase-1 enum in `app/v2/enums.py` uses
`QUEUE_RETRY` / `MARK_FAILED` / `CLEAR_CLAIM`. Phase 4 uses the
phase-1 names (authoritative — they came from the round-7
review). The design doc has stale wording in §4.0.4 row 5.

Resolution: phase 4 plan documents the mapping; a tiny
design-doc resync commit may follow (gated on Sergey approval
to edit the locked design doc).

### 5.3 Tests

- Empty DB → empty list.
- Pending + due_at past → NOT recovered (only claimed/running
  are stale candidates).
- Claimed within timeout → not recovered.
- Claimed past timeout → recovered, marked failed, event row
  emitted.
- Running within timeout → not recovered.
- Running past timeout → recovered, NEW pending run inserted
  with `parent_run_id = stale_run.id`, `root_run_id` carried,
  `attempt = stale.attempt + 1`, fire_reason=retry. Original
  stale run is NOT re-statused (terminal at failed).

Wait — design §4.0.4 says the prior-running stale run gets
queued for retry. But terminal "failed" is forever per
round-6. So actually the stale running run is marked
**failed** (terminal) AND a new pending row is inserted as the
retry. Two-row remediation. Pin this carefully in tests.

### 5.4 Timeout

Single argument; caller-controlled. No default in phase 4 —
forces the caller to think about it. Production wiring picks
a value (probably 5 minutes for `claimed`, longer for
`running`).

---

## 6. Worker loop

### 6.1 Module API

```python
# app/v2/runtime/worker.py

class Worker:
    """Async task that polls for due pending runs and
    transitions them through the empty execution path.

    Construction:
        Worker(
            conn_factory: Callable[[], sqlite3.Connection],
            worker_id: str,
            *,
            poll_interval: timedelta,
        )

    The factory pattern lets the worker open its own dedicated
    connection without the storage layer ever taking an
    implicit dependency on data/ori-scheduler.db. Production
    wires the factory to point at the prod DB; tests wire it
    to a tmp_path connection.

    Lifecycle:
        await worker.start()   # spawns the poll loop
        await worker.stop()    # cooperative shutdown

    Per-tick:
        1. list_pending_due(now, limit=1)
        2. claim_run(...)
        3. on True: mark_run_status(claimed → running)
           append run_started event in same TX.
        4. (empty body — await asyncio.sleep(0))
        5. update_run_status_and_append_event(running → succeeded,
           kind=run_succeeded).
        6. Sleep poll_interval.
        7. Exit cleanly when stop() called.

    Phase 4 worker NEVER calls into reasoning or emit. The body
    is a no-op marker for the lifecycle to flow through.
    """
```

### 6.2 Wakeup callback

```python
# app/v2/runtime/wakeup.py

def wakeup(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    now: datetime,
) -> list[str]:
    """Read the ScheduleSpec for schedule_id, compute due_at(s)
    based on its trigger, INSERT one or more pending Run rows
    + matching run_created events in a single TX. Returns the
    inserted run ids.

    No-ops when:
      - the schedule does not exist
      - the schedule is paused/archived
      - the trigger is not yet due

    Trigger semantics:
      - OneOffTrigger: insert one run at the configured
        at_iso_datetime; cleanup is the registration layer's
        job (later phase).
      - CronTrigger: insert one run for the current fire time
        (cron parsing per the installed croniter or
        APScheduler util; phase 4 makes a deliberate decision
        below).
      - IntervalTrigger / EventTrigger / ConditionalTrigger:
        stubbed in phase 4 — raise NotImplementedError. Real
        wakeup wiring lands when their use cases ship.

    The callable is the function APScheduler will eventually
    register. Phase 4 does NOT actually register it — tests
    invoke it directly with a synthetic ``now``.
    """
```

Cron parsing decision (phase 4): use the cron library already
present in the v1 scheduler if available; otherwise add a tiny
helper that computes "is this cron due at this `now`" without
pulling in APScheduler at runtime. Reviewer to pick.

### 6.3 APScheduler binding

Per design §4.0.3, APScheduler keeps managing only its
`apscheduler_jobs` table. The wakeup function is the callback
registered against each schedule. Phase 4 ships only the
function; the registration glue (start scheduler, register
all active schedules, hook into pause/archive/revive) lands
in the slice that introduces production wiring — probably
phase 5 or in a dedicated slice at the end of phase 4 if
reviewer says go. Default plan: **defer to a phase-4 closeout
slice** if all earlier slices are clean.

### 6.4 Tests

- Worker single-tick: pending → claimed → running → succeeded
  with 4 event rows (run_created not from worker; worker emits
  run_claimed + run_started + run_succeeded).
- Worker tick with no pending runs returns cleanly + sleeps.
- Worker tick on a paused schedule does NOT claim
  (claim_run's single-flight predicate handles this only if
  the schedule's pending run was already inserted; the wakeup
  callback skips paused schedules at INSERT time, not the
  worker).
- Concurrent workers (two `Worker` instances, distinct
  worker_id) on the same DB: each claims a distinct pending
  run for distinct schedules. Single-flight predicate is what
  keeps them off the same run / same schedule.
- Worker cooperative shutdown: stop() pending the current
  tick to finish.
- Worker body NEVER calls anything outside the storage layer
  + asyncio.sleep. Smoke test asserts the module's transitive
  imports don't include httpx / requests / etc.

### 6.5 Wakeup tests

- OneOff at_iso_datetime past `now` → no insert.
- OneOff at_iso_datetime <= `now` → insert one pending Run +
  run_created event in same TX.
- Cron `*/5 * * * *` with `now` aligned → insert.
- Cron with `now` between fire times → no insert.
- Paused schedule → no insert regardless of trigger.
- Archived schedule → no insert.
- Unknown schedule id → no insert (returns []).
- IntervalTrigger / EventTrigger / ConditionalTrigger →
  NotImplementedError (deliberately, with explicit message
  pointing at the phase that will land them).

---

## 7. Test inventory summary

| Module | tests |
|---|---|
| `test_runtime_state_machine.py` | legal-transitions exhaustive coverage; illegal-transition raises; LEGAL_TRANSITIONS pin |
| `test_runtime_claim.py` | single-flight per schedule; cross-schedule independence; naive now; atomicity-on-failure |
| `test_runtime_recovery.py` | empty DB; claimed within/past timeout; running within/past timeout; two-row remediation for running stale |
| `test_runtime_worker.py` | single-tick state-machine walk; no-pending tick; concurrent workers; cooperative shutdown; smoke check on imports |
| `test_runtime_wakeup.py` | OneOff past/future; Cron aligned/misaligned; paused; archived; unknown id; NotImplementedError on Interval/Event/Conditional |

Cross-cutting smoke checks (carried from phase 3 pattern):
- `app.v2.runtime.*` imports no I/O libraries (httpx, requests,
  slack_sdk, googleapiclient, etc.). `asyncio` IS allowed —
  the worker loop is async.
- No public callable in any phase-4 module dispatches to
  reasoning / emit / sub-agents — the body is empty by design.
  Phase-4 smoke test forbids `reason`, `emit`, `delegate`,
  `transfer` callable names (these belong to later phases).

---

## 8. CI guard checks

`PHASE_ALLOWLIST[4]` mirrors phase 3's machinery surface, with
`docs/PHASE_4_PLAN.md` replacing `docs/PHASE_3_PLAN.md`.

Phase-3 plan stays out of the phase-4 allowlist; if a phase-3
doc fix is genuinely needed during phase 4, use the
`PHASE_OVERRIDE:` mechanism.

The phase guard's smoke-test set for phase-3 storage modules
(forbidding `claim`, `claim_run`, `worker`, `dispatch`, etc.)
is **a per-module test concern** in those test files — it does
NOT carry into phase-4 module tests, because `claim_run`,
`Worker`, etc. are legitimate names in phase 4. New runtime
test modules ship their own forbidden set tailored to phase 4
(forbid `reason`, `emit`, `delegate`, `transfer`,
`sub_agent`, etc.).

---

## 9. Commit ordering

Test-first invariant from design §11.3: every code commit
ships with matching tests in the same commit. Suggested order
(reviewer / Sergey may override):

| # | scope |
|---|---|
| 0 | this plan + phase bump + allowlist widening |
| 1 | state machine module + tests |
| 2 | claim_run primitive + tests (single-flight regression suite) |
| 3 | recovery scan + tests |
| 4 | worker loop with empty body + tests |
| 5 | wakeup callback + tests |
| 6 | (optional) APScheduler binding glue + integration test |
| close | acceptance criteria verified + tag |

Each slice pauses for reviewer.

---

## 10. Acceptance criteria

Phase 4 complete when ALL true:

1. All files in §2 exist.
2. `uv run python -m pytest tests/v2 --tb=short -q` passes
   with zero failures.
3. Full suite passes (no regressions).
4. `uv run python scripts/check_phase_scope.py --diff
   v2-phase-3-complete` reports no violations (or only the
   acknowledged side-track override marker if it persists).
5. Every legal transition in §3.1 has a passing
   round-trip test (worker walks the path or
   `assert_legal_transition` allows it).
6. Every illegal transition has a passing rejection test.
7. Claim single-flight predicate is exercised under all
   four scenarios in §4.3.
8. Recovery scan handles both the claimed-stale and
   running-stale cases per §5.3, including the two-row
   remediation for running.
9. Worker walks pending → claimed → running → succeeded
   under a synthetic clock with no I/O imports.
10. Wakeup callback handles OneOff + Cron + the three
    stubbed-NotImplementedError trigger types.
11. Smoke test confirms no `reason` / `emit` / `delegate` /
    `transfer` callables anywhere in `app/v2/runtime/*` — the
    worker body remains a no-op until later phases wire real
    execution.
12. Annotated git tag `v2-phase-4-complete` created and
    pushed (gated on explicit Sergey approval).

---

## 11. Tag annotation

```
v2 phase 4 complete

Adds the runtime skeleton over phase-3 storage:
state-machine transition rules, single-flight claim_run,
boot-time recovery scan, async worker loop with empty
execution body, wakeup callback function.

NO reasoning, NO emit, NO real APScheduler scheduler
construction. The worker walks pending → claimed → running
→ succeeded without doing any external work — real
execution lands in later phases. v1 paths untouched.
Production DB never accessed implicitly.

Design: docs/CONTRACTS_V2_DESIGN.md §4.0.1, §4.0.4, §6.1, §12 step 4
Plan:   docs/PHASE_4_PLAN.md
```

---

## 12. Open questions

1. **RecoveryPolicy enum naming drift**: phase 1 enum
   (`QUEUE_RETRY` / `MARK_FAILED` / `CLEAR_CLAIM`) vs design
   §4.0.4 row 5 wording (`RETRY_PENDING` / `MARK_FAILED` /
   `RESUME`). Phase 4 uses the phase-1 names; design doc
   resync gated on Sergey approval.

2. **Cron parsing source**: APScheduler's util, croniter, or
   a tiny in-house helper? Reviewer to pick before slice 5.
   Default proposal: APScheduler's `CronTrigger` parsing,
   imported as a pure parser without spawning a scheduler.

3. **APScheduler binding glue location**: phase-4 closeout
   slice OR deferred to phase 5? Reviewer call after slice 5
   lands.

4. **Worker connection lifetime**: per-tick connection OR
   one connection for the worker's whole lifetime? SQLite
   single-writer means a long-lived connection per worker is
   the standard pattern, but stale connection recovery (e.g.
   restart after migration) might prefer per-tick. Plan
   proposes long-lived; reviewer can push back.
