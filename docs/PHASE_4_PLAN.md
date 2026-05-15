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
   `RunStatus` transitions that phase-4 code actually
   performs. Other transitions (manual clear-claim, paused-
   pending cancellation) are documented as future paths and
   are NOT in the phase-4 LEGAL_TRANSITIONS table.
2. **Claim primitive** — `claim_run(conn, run_id, *, claimed_by,
   now, event_id) → bool`. Wraps the design §6.1 single-flight
   UPDATE PLUS a schedule-status predicate so paused / archived
   schedules cannot have their pending runs claimed.
   ``event_id`` is the injected id for the paired
   ``run_claimed`` event row.
3. **Recovery scan** — boot-time helper that finds stale
   claimed / running rows past their per-status timeout and
   applies ``RecoveryPolicy.QUEUE_RETRY`` to both cases (the
   stale row is marked ``failed`` terminal + a fresh pending
   retry row is inserted with parent_run_id /
   root_run_id-carried / attempt+1 / fire_reason='retry').
   Two cursors: ``claimed_at`` for claimed-stale,
   ``started_at`` for running-stale. Failures surface as
   `RecoveryError` items in the result list — never silently
   dropped.
4. **Worker loop** — long-running async task that polls
   `list_pending_due`, claims a run, transitions
   `claimed → running → succeeded` (empty body), recording
   matching events for every transition. The body itself is
   `await asyncio.sleep(0)` or equivalent no-op — real
   reasoning + emit lands in later phases.
5. **Wakeup callback** — `wakeup(conn, *, schedule_id, now,
   run_id_factory, event_id_factory) → list[str]`. Reads the
   ScheduleSpec, computes any due `due_at`(s), INSERTs Run
   rows + matching `run_created` events in the same TX.
   Returns the inserted run ids. Callable directly (testable
   in isolation). ``run_id_factory`` / ``event_id_factory``
   are required injectables — no implicit ``uuid.uuid4()`` at
   this layer.
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

Phase 4 ``LEGAL_TRANSITIONS`` contains EXACTLY the transitions
phase-4 code actually performs. Anything outside this set
raises ``IllegalTransitionError``.

```
pending   → claimed     (worker wins single-flight claim)
claimed   → running     (worker has started the body)
claimed   → failed      (recovery: mark abandoned claim)
running   → succeeded   (worker body completed)
running   → failed      (worker body raised, OR recovery
                         marks abandoned running)
```

Terminal states: `succeeded`, `failed`, `cancelled`. Retries
land as NEW pending Run rows with `parent_run_id` set, NOT by
re-statusing a failed row (round-6 invariant; storage layer's
RunStatus CHECK enforces no `retry_pending`).

### 3.1.1 Transitions deliberately NOT in phase 4

The earlier draft of this plan listed `claimed → pending`,
`running → pending`, and `pending → cancelled` as legal — that
was wrong. Each is removed because no phase-4 code performs
them:

| Transition | Future owner |
|---|---|
| `claimed → pending` | Admin tool using `RecoveryPolicy.CLEAR_CLAIM` (manual override only — design §4.0.4). Ships in a later admin slice or phase. |
| `running → pending` | Retry chain construction inserts a NEW pending row (different Run id) rather than re-statusing the old one. The stale running row goes to `failed` per §5. |
| `pending → cancelled` | Pause / archive enforcement per design §4.0.4 + the `paused_pending_policy` field on a schedule. Lands when the pause/archive admin path ships (later phase). |

These are documented for future implementers; including them
in phase 4's table would create real re-execution risk
(running → pending makes the same Run row eligible for a
second worker claim).

### 3.2 Module API

```python
# app/v2/runtime/state_machine.py

class IllegalTransitionError(ValueError):
    """Raised when a caller attempts a status change that
    isn't in the legal-transitions table."""

LEGAL_TRANSITIONS: frozenset[tuple[RunStatus, RunStatus]] = frozenset({
    (RunStatus.PENDING, RunStatus.CLAIMED),    # worker claim
    (RunStatus.CLAIMED, RunStatus.RUNNING),    # worker body start
    (RunStatus.CLAIMED, RunStatus.FAILED),     # recovery: abandoned claim
    (RunStatus.RUNNING, RunStatus.SUCCEEDED),  # worker body completed
    (RunStatus.RUNNING, RunStatus.FAILED),     # worker raised, or recovery
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
    event_id: str,
) -> bool:
    """Attempt to atomically claim a pending run.

    Returns True iff this caller owns the run after the call;
    False iff another worker won the race OR the run is no
    longer pending OR a concurrent run on the same schedule
    is already claimed/running (single-flight).

    Implementation runs the design §6.1 UPDATE, EXTENDED with
    a schedule-status predicate (per reviewer round-2 fix):

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
          AND EXISTS (
            SELECT 1 FROM schedules s
            WHERE s.id = runs.schedule_id
              AND s.status = 'active'
          )

    The schedule-status check closes the **paused-schedule
    claim gap**: pending Runs that were INSERTed before a
    pause must NOT be claimed by a worker that sees the
    schedule as paused/archived. The check happens INSIDE the
    same atomic UPDATE — no TOCTOU window. SQLite WAL
    serialises writers; the single-flight + schedule-status
    predicates evaluate together with the UPDATE.

    What this does NOT do: it does NOT mark already-pending
    runs as ``cancelled`` when their schedule pauses. That's
    the ``paused_pending_policy`` decision (let_complete vs
    cancel_pending) which lands when the pause/archive admin
    path ships. Phase 4's behavior: pause leaves pending rows
    untouched; claim refuses them; if the schedule resumes
    later the runs become claimable again.

    On successful claim, the helper writes the ``run_claimed``
    Event in the same transaction (uses
    transactions.transaction(conn) + append_event from phase 3).
    Atomicity-on-failure: if the event INSERT fails the claim
    UPDATE rolls back.

    ``event_id`` is injected — the helper does NOT call
    ``uuid.uuid4()`` itself. Worker passes
    ``event_id_factory()`` at the call site. Tests inject a
    deterministic counter.
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

- Single pending run + active schedule → claim_run returns
  True; status flips to `claimed`; `claimed_by` + `claimed_at`
  set; `run_claimed` event appended.
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
- **Paused schedule + pending run → claim_run returns False,
  run stays pending, no `run_claimed` event written.** This
  is the regression pin for the paused-schedule claim gap.
- **Archived schedule + pending run → claim_run returns
  False, run stays pending, no event leaked.**
- Schedule transitions paused → active mid-test → previously
  refused run becomes claimable.
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
    """A successfully recovered row."""
    run_id: str
    schedule_id: str
    prior_status: RunStatus            # claimed OR running
    applied_policy: RecoveryPolicy
    new_pending_run_id: Optional[str]  # set when policy
                                       # inserted a retry row

@dataclass(frozen=True)
class RecoveryError:
    """A row the scan attempted to remediate but couldn't.

    Returned alongside ``RecoveredRun`` so the caller sees
    every failure — the scan never silently drops a remediation.
    ``logger.error`` is also called inside the helper; the
    structured result is what the caller iterates for
    follow-up (admin alert, retry, etc.).
    """
    run_id: str
    schedule_id: str
    prior_status: RunStatus
    error_message: str

def scan_stale_runs(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    claimed_timeout: timedelta,
    running_timeout: timedelta,
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
) -> list[Union[RecoveredRun, RecoveryError]]:
    """Boot-time scan + remediation.

    The scan uses TWO timestamp cursors (reviewer round-2 fix
    — earlier draft used ``claimed_at`` for both, which can
    false-recover a long-queued claimed row whose body just
    hasn't started yet):

      - claimed-stale = ``status='claimed'`` AND
        ``claimed_at < now - claimed_timeout``.
      - running-stale = ``status='running'`` AND
        (``started_at < now - running_timeout`` OR
         ``started_at IS NULL``).
        The IS-NULL fallback is defensive — phase-4 invariant
        says a row in ``running`` status MUST have
        ``started_at`` set, but if the invariant ever
        regresses the scan treats null as immediately stale
        rather than silently never-recovering.

    Remediation policy per prior status (reviewer round-3
    correction — claimed-stale now QUEUE_RETRY, not
    MARK_FAILED):

      - prior = claimed  → RecoveryPolicy.QUEUE_RETRY.
        Worker died AFTER claiming but BEFORE the body
        started. The body never ran, so no external work was
        performed and no emit fired. Re-firing is safe (no
        duplicate-side-effect risk); not re-firing trades
        duplicate safety we don't need for a missed task.
        For reminder-style schedules a missed fire is the
        worse failure mode. Two-row remediation in a single
        TX, same shape as running-stale below:
          1. UPDATE the stale row to ``failed`` (terminal —
             round-6 invariant: no re-statusing a row out of
             a terminal state; the retry is a NEW row).
          2. INSERT a NEW pending Run row with
             parent_run_id = stale_run.id,
             root_run_id = stale_run.root_run_id (carried),
             attempt = stale_run.attempt + 1,
             fire_reason = 'retry',
             due_at = now.
          3. Append matching events: run_failed +
             run_retry_scheduled.
      - prior = running  → RecoveryPolicy.QUEUE_RETRY.
        Same two-row remediation as claimed-stale. Caveat:
        body started, side effects MAY have partially fired
        (a real worker writes emit_succeeded events as they
        go; the retry path relies on the ledger's
        ``get_last_emit_succeeded`` dedup to skip already-
        delivered emits). Phase 4's worker has an empty body,
        so this corner does not bite until later phases wire
        real emit.

    Each remediation runs in its own ``transaction(conn)``
    block. **Non-silent failure**: a remediation that raises
    catches at the boundary, calls ``logger.error(...)`` with
    the full context, and appends a ``RecoveryError`` to the
    result list. The scan continues with the next row.
    The caller iterates the result list, sees both successes
    and errors, and decides whether to alert / retry / abort.

    The third RecoveryPolicy value (CLEAR_CLAIM) is reserved
    for an admin tool that flips a stuck claimed row back to
    pending without bumping attempt. Phase 4 does NOT expose
    that path.

    ``run_id_factory`` / ``event_id_factory`` are injected —
    the helper does NOT call ``uuid.uuid4()`` itself. Caller
    (boot sequence) wires production factories; tests inject
    deterministic counters so the inserted retry rows and
    paired events have stable ids assertable in tests.
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

### 5.2.1 Claimed-stale policy deviation from design default

Reviewer round-3 push-back: design §4.0.4 row 5 says claimed-
stale default is ``MARK_FAILED`` ("refuse to re-execute"). The
plan now uses ``QUEUE_RETRY`` for claimed-stale.

Reasoning (reviewer + my read):
- Claimed means worker died AFTER the claim UPDATE but BEFORE
  the row transitioned to ``running``. The body never ran;
  no emit fired; no external side effect occurred. Re-firing
  is safe (nothing to dedupe against).
- For reminder-style schedules, a missed fire is the worse
  failure mode than a duplicate would be. Design's default
  trades duplicate-safety we don't have any exposure to.

Counter-concern (not yet addressed in plan):
- For high-cadence cron schedules ("every minute"), a retry
  queued at ``due_at = now`` can briefly produce 2× output
  (the retry + the next regular tick). Design §4.0.4 alludes
  to ``backfill_policy`` for this, but the plan doesn't wire
  it. Phase 4's empty worker body neutralises the visible
  impact — no real cadence regression possible until later
  phases wire emit. The right per-schedule recovery policy
  (and backfill policy) lands when those phases ship.

**Sergey to confirm before slice 1.** If the design-doc
default takes precedence, flip claimed-stale back to
MARK_FAILED in §5.1 + §5.3. Either way, the helper signature
+ result structure stay the same.

### 5.3 Tests

- Empty DB → empty list.
- Pending + due_at past → NOT recovered (only claimed/running
  are stale candidates).
- Claimed within `claimed_timeout` → not recovered.
- Claimed past `claimed_timeout` → RecoveredRun with
  applied_policy=QUEUE_RETRY (reviewer round-3); two-row
  remediation: stale row flipped to ``failed`` (terminal),
  NEW pending row inserted with parent_run_id,
  root_run_id carried, attempt += 1, fire_reason='retry',
  due_at=now. Events: run_failed + run_retry_scheduled.
  RecoveredRun.new_pending_run_id populated.
- Running within `running_timeout` (started_at recent) → not
  recovered.
- Running past `running_timeout` (started_at past) → same
  two-row remediation as claimed-stale; RecoveredRun.
  applied_policy=QUEUE_RETRY.
- Running with `started_at IS NULL` (invariant violation
  defensive case) → treated as immediately stale; same
  two-row remediation.
- Mixed cursor regression: a claimed-stale row whose
  claimed_at is past but whose timeout corresponds to the
  RUNNING threshold must NOT recover (each cursor is
  independent).
- Remediation failure non-silent: monkey-patch the helper to
  raise mid-remediation. Result list contains a RecoveryError
  for that row + the helper's `logger.error` was called.
  Subsequent rows still attempted.

### 5.4 Timeouts

Two arguments now (claimed_timeout, running_timeout). No
defaults — forces the caller to think about it. Production
wiring picks values; reasonable defaults documented in code
comments (probably 5 minutes for claimed, 30 minutes for
running, but configurable per deployment).

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
            clock: Callable[[], datetime],          # required
            run_id_factory: Callable[[], str],      # required
            event_id_factory: Callable[[], str],    # required
        )

    Three injected dependencies, all required (no defaults at
    the class level — production wiring picks them once, tests
    pick deterministic ones):

    - ``conn_factory`` opens a SQLite connection. Production
      wires the prod DB path; tests wire ``tmp_path``.
    - ``clock`` returns the current ``datetime`` (tz-aware).
      Tests inject a synthetic clock so recovery timestamps
      and claim_at values are deterministic.
    - ``run_id_factory`` / ``event_id_factory`` produce new ids
      for any Run / Event rows the worker inserts. Production
      wires ``uuid.uuid4().hex``; tests wire a counter that
      yields ``run-1``, ``run-2``, ... so assertions on inserted
      rows have stable values.

    The worker NEVER calls ``datetime.now()`` or ``uuid.uuid4()``
    directly. Every timestamp comes from ``clock()``; every
    new id comes from one of the two factories. A smoke test
    pins this by grepping the module for raw calls.

    Lifecycle:
        await worker.start()   # spawns the poll loop
        await worker.stop()    # cooperative shutdown

    Per-tick:
        1. list_claimable_due(now=clock(), limit=claim_batch_size)
           — see §6.1.1 for the deviation from the original
           `list_pending_due(limit=1)` design.
        2. For each candidate in the batch (in due_at order):
           call claim_run(..., now=clock(),
           event_id=event_id_factory()). Stop on the first
           successful claim. Refusals here are race losses
           only (the §6.1.1 pre-filter eliminates structural
           refusal cases) — the tick walks to the next
           candidate.
        3. on True: update_run_status_and_append_event walks
           the row claimed → running with kind=run_started,
           event id from event_id_factory(), and stamps
           started_at=clock(). Gated by
           assert_legal_transition(CLAIMED, RUNNING).
        4. (empty body — await asyncio.sleep(0))
        5. update_run_status_and_append_event walks running →
           succeeded with kind=run_succeeded, event id from
           event_id_factory(), and stamps
           completed_at=clock(). Gated by
           assert_legal_transition(RUNNING, SUCCEEDED).
        6. Sleep poll_interval.
        7. Exit cleanly when stop() called.

    Phase 4 worker NEVER calls into reasoning or emit. The body
    is a no-op marker for the lifecycle to flow through.
    """
```

### 6.1.1 Claimable-due pre-filter (round-5 deviation)

The original plan said `list_pending_due(limit=1)`. That
turned out to be load-bearing for starvation: a non-claimable
row at the head of the pending queue (paused/archived
schedule, or same-schedule already claimed/running) would
re-appear on every tick and the worker would never reach
active pending rows behind it. Bumping to a bounded batch
narrowed but did not fix the bug — N+1 blocked rows still
starve everything behind them.

The fix (per reviewer round-5):

1. Phase 3 storage layer gains
   ``list_claimable_due(conn, *, now, limit)`` — strictly
   stronger than ``list_pending_due``. SQL filters add
   ``EXISTS schedules WHERE status='active'`` and
   ``NOT EXISTS runs r2 WHERE r2.schedule_id =
   runs.schedule_id AND r2.id != runs.id AND r2.status IN
   ('claimed', 'running')`` — exactly the predicates
   ``claim_run`` itself enforces.
2. Worker constructor gains
   ``claim_batch_size: int = 10`` (must be >= 1). The tick
   reads up to that many claimable-due rows and walks the
   batch in due_at order until ``claim_run`` succeeds. The
   batch is purely for race resilience now — non-claimable
   rows never enter it at the SQL layer.
3. ``claim_run`` remains the race-safe final gate. Refusals
   inside the worker loop are race losses (another worker
   won between our read and our claim) — not structural
   blocks.

Regression tests pin both halves: a storage-level set
asserts ``list_claimable_due`` filters paused / archived /
single-flight-blocked rows; a worker-level test seeds 11
paused-older pending rows + 1 active-newer row with the
default ``claim_batch_size=10`` and asserts the worker still
processes the active row on the first tick.

### 6.2 Wakeup callback

```python
# app/v2/runtime/wakeup.py

def wakeup(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    now: datetime,
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
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
        at_iso_datetime when ``at_iso_datetime <= now``;
        cleanup is the registration layer's job (later phase).
      - CronTrigger: ships in a SEPARATE slice after the
        OneOff path lands. Cron parsing source is an open
        question (reviewer to pick between APScheduler's
        ``CronTrigger.from_crontab`` and ``croniter``).
        **No in-house cron parser** — using the same parser
        v1 already trusts avoids a whole class of date-math
        regressions.
      - IntervalTrigger / EventTrigger / ConditionalTrigger:
        stubbed in phase 4 — raise NotImplementedError. Real
        wakeup wiring lands when their use cases ship.

    The callable is the function APScheduler will eventually
    register. Phase 4 does NOT actually register it — tests
    invoke it directly with a synthetic ``now``.

    ``run_id_factory`` / ``event_id_factory`` injected — same
    rule as scan_stale_runs (helper does not call
    ``uuid.uuid4()``).
    """
```

**Cron parsing decision (closed, slice 5b):** APScheduler's
``CronTrigger.from_crontab(cron, timezone=ZoneInfo(...))``.
Already a dependency in v1; not in-house.

Fire-detection is forward-only: phase-4 wakeup calls
``aps_trigger.get_next_fire_time(None, now_local)`` and
inserts a Run only when the returned instant equals ``now``
on the UTC time line. No "most recent past fire" semantics.
Late wakeups are NOT backfilled — that lives in the design's
``backfill_policy`` and ships in later phases.

**DOW name-only constraint:** APScheduler interprets numeric
day-of-week as ``Monday=0`` while standard Unix cron uses
``Sunday=0``. Authors trained on Unix cron expecting
``0 18 * * 1-5`` = "Mon-Fri" would silently get "Tue-Sat".
To eliminate the ambiguity, v2 cron triggers MUST express the
DOW field as ``*`` or named days (``MON``, ``TUE``, ...) with
range / list / step syntax — any digit in field 5 is rejected
at the wakeup layer BEFORE APScheduler sees the expression.
APScheduler does NOT accept ``?`` for day_of_week, so that
form is also unavailable in practice.

### 6.2.1 Clock + id-factory injection rule (cross-cutting)

EVERY phase-4 runtime helper that needs a timestamp takes
``now`` (or ``clock`` in long-lived constructors). EVERY helper
that inserts a row needing a new id takes
``run_id_factory`` / ``event_id_factory`` callables. No phase-4
runtime module imports ``datetime.datetime`` for ``now()`` or
``uuid`` for ``uuid4()`` directly — the smoke tests grep for
those calls and fail loud if added.

Production wiring (NOT in phase 4):
- ``clock = lambda: datetime.now(timezone.utc)``
- ``run_id_factory = event_id_factory = lambda: uuid.uuid4().hex``

A single ``app/v2/runtime/_defaults.py`` module SHIPS later
alongside the APScheduler binding glue. Phase 4 forces every
helper to accept the injection explicitly.

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
- Worker tick on a paused schedule does NOT claim. The
  ``schedule-status predicate`` inside ``claim_run`` (§4.1
  UPDATE clause ``EXISTS schedules WHERE status='active'``)
  catches pending rows that pre-dated a pause; the wakeup
  callback (§6.2) refuses to insert NEW pending rows for a
  paused schedule.
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

**Slice 5a (OneOff only — no cron parser dependency):**

- OneOff `at_iso_datetime > now` → no insert.
- OneOff `at_iso_datetime <= now` → insert one pending Run +
  run_created event in same TX.
- Paused schedule + any trigger → no insert.
- Archived schedule + any trigger → no insert.
- Unknown schedule id → no insert (returns []).
- CronTrigger schedule passed to slice-5a wakeup → raises
  ``NotImplementedError`` (slice 5b not yet shipped). Pinning
  this so a coder who runs 5a doesn't accidentally implement
  cron alongside.
- IntervalTrigger / EventTrigger / ConditionalTrigger →
  NotImplementedError (deliberately, with explicit message
  pointing at the phase that will land them).
- Injected ``run_id_factory`` / ``event_id_factory`` are
  invoked exactly once per inserted row / event; pin via a
  counter factory.

**Slice 5b (Cron, parser = APScheduler):**

- Cron `0 18 * * *` aligned to ``now`` → insert one pending
  Run + run_created event in the same TX.
- Cron `*/5 * * * *` aligned to a 5-minute boundary → fires.
- Cron `0 18 * * MON-FRI` on a Friday at the aligned instant
  → fires.
- Cron in a non-UTC timezone (Europe/Kyiv 18:00 = 15:00 UTC
  in summer) → fires when wakeup ``now`` is the matching UTC
  instant.
- Cron `now == 18:01` (one minute past fire) → no insert,
  factories never called.
- Cron `now == 17:59` (one minute before fire) → no insert.
- Cron + paused / archived schedule → no insert (carries
  forward the 5a guard rails). Factories never called — the
  schedule-status short-circuit precedes the cron parser.
- Numeric DOW (`1-5`, `0,6`, `*/2`, `MON-FRI/2`, single
  digit) rejected at the wakeup layer with ``ValueError``;
  factories never called.
- Named DOW (`*`, `FRI`, `MON-FRI`, `MON,WED,FRI`) accepted.
  ``?`` is NOT supported because APScheduler refuses it.
- Bad timezone string → ``ValueError`` (no APScheduler call).
- Invalid cron expression (e.g. `99 18 * * *`) → ``ValueError``
  with the underlying APScheduler message attached.
- Hand-built 6-field cron (bypassing the Pydantic 5-field
  validator via direct trigger_json overwrite) fails loud at
  the storage re-parse layer.

---

## 7. Test inventory summary

| Module | tests |
|---|---|
| `test_runtime_state_machine.py` | legal-transitions exhaustive coverage; illegal-transition raises; LEGAL_TRANSITIONS pin |
| `test_runtime_claim.py` | single-flight per schedule; cross-schedule independence; naive now; atomicity-on-failure |
| `test_runtime_recovery.py` | empty DB; claimed within/past timeout; running within/past timeout; two-row remediation for running stale |
| `test_runtime_worker.py` | single-tick state-machine walk; no-pending tick; concurrent workers; cooperative shutdown; smoke check on imports |
| `test_runtime_wakeup.py` | OneOff past/future; paused; archived; unknown id; Interval / Event / Conditional → NotImplementedError; injected id factories invoked once per inserted row/event. |
| `test_runtime_wakeup_cron.py` (5b) | Cron aligned/misaligned with `now`; UTC + non-UTC timezone alignment; named DOW accepted; numeric DOW rejected (5 parametrised forms); invalid cron / timezone surfaces ValueError; paused / archived still no-op. |

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
| 2 | claim_run primitive (with schedule-status predicate) + tests (single-flight + paused-claim regressions) |
| 3 | recovery scan (two-cursor: claimed_at / started_at) + non-silent RecoveryError result + tests |
| 4 | worker loop with empty body + tests |
| 5a | wakeup OneOff path + tests |
| 5b | wakeup Cron path + tests (after parser choice confirmed) |
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
   `assert_legal_transition` allows it). §3.1.1 future
   transitions are NOT in `LEGAL_TRANSITIONS`.
6. Every illegal transition has a passing rejection test,
   including the §3.1.1 future transitions (must raise in
   phase 4).
7. Claim single-flight predicate AND the schedule-status
   predicate are exercised: paused / archived schedules
   refuse claim, then become claimable after a transition
   back to active.
8. Recovery scan uses TWO cursors (claimed_at /
   started_at); each is tested independently. Two-row
   remediation pinned for running-stale. Failed remediations
   surface as `RecoveryError` items in the result list
   (non-silent failure).
9. Worker walks pending → claimed → running → succeeded
   under a synthetic clock with no I/O imports.
10. Wakeup callback handles OneOff in slice 5a. Cron lands
    in 5b after the parser choice is confirmed. Interval /
    Event / Conditional → NotImplementedError.
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

2. **Cron parsing source**: APScheduler's
   `CronTrigger.from_crontab` or `croniter`. **NOT in-house**
   (reviewer round-2 — too risky to roll a custom parser).
   Reviewer to pick BEFORE slice 5b.

3. **APScheduler binding glue location**: phase-4 closeout
   slice OR deferred to phase 5? Reviewer call after slice 5
   lands.

4. **Worker connection lifetime**: per-tick connection OR
   one connection for the worker's whole lifetime? SQLite
   single-writer means a long-lived connection per worker is
   the standard pattern, but stale connection recovery (e.g.
   restart after migration) might prefer per-tick. Plan
   proposes long-lived; reviewer can push back.

5. **paused_pending_policy implementation**: when does a
   schedule's pause actually mark existing pending runs as
   cancelled? Phase 4's claim check refuses them; the
   cancellation transition (`pending → cancelled`) is owned
   by the future pause/archive admin path. Reviewer +
   Sergey to decide whether that lives in phase 4 closeout
   or a later phase. Default: defer.
