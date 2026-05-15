# Scheduler v2 — Phase 5 plan

Phase 4 shipped 2026-05-15 (tag `v2-phase-4-complete`). Phase 5
introduces the production-wiring layer around the phase-4
runtime: APScheduler construction + lifecycle, schedule
registration, boot sequence (recovery scan + worker pool
start), and pause/archive/resume hooks.

This phase deliberately stops short of cutting over from v1.
The cutover to v2 for production schedules happens at phase 9
(`OneOffReminder` end-to-end per design §12.1 invariant 3,
post-renumber). Phase 5 makes the v2 runtime **startable**
for tests / dev runs, hooks the schedule-status lifecycle
into APScheduler, and leaves `run_bot.py` untouched.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §4.0.3 (APScheduler-as-wakeup-
  only scope), §4.0.5 (crash / restart recovery), §12 step 4
  + step 5.
- `docs/PHASE_4_PLAN.md` §6.1 (Worker constructor),
  §6.2 (wakeup), §6.3 (APScheduler binding deferral default),
  §12 open questions 3 + 5.

---

## 0. Design-doc amendment (applied in this commit)

Design `docs/CONTRACTS_V2_DESIGN.md` §12 step 5 previously
read **"Registry cache for channels/sheets/docs"**. The
phase-4 closeout deferred APScheduler binding out of phase 4
per Sergey's 2026-05-15 call, leaving the binding work
unowned by any §12 step. This plan applies the resolution
**in the same commit** (binding is a code path; the design
must reflect it before code lands):

- A new §12 step 5 **"APScheduler binding + boot sequence"**
  is inserted between step 4 (APScheduler-as-wakeup-only +
  Run claim) and the former step 5 (Registry cache).
- Former steps 5–16 are renumbered 6–17.
- The dependency block at the bottom of §12 is updated to
  match the new numbering.

The runtime sequencing argument: design §12 step 4
explicitly defers the binding ("APScheduler-as-wakeup-only…
wakeup callback that inserts pending Runs"); §12.1
invariant 2 then holds it back until later. Phase 4 plan
§6.3 picked "defer to phase 5" as the default and shipped
clean. The new step formalises that deferral in the design
contract itself.

Alternatives that were considered + rejected:

- **Rename to "Phase 4 closeout (binding)"** — would keep
  the design contract numbering intact but leave the
  binding work outside §12's tagged sequence. Rejected
  because the binding is its own logical change with its
  own tag (`v2-phase-5-complete`); folding it into phase 4
  retroactively muddles the per-phase revert story.
- **Skip binding until phase 9 cutover** — phase 6 / 7 / 8
  work (registry cache, authoring tools, dry-run) would
  have no way to exercise the wakeup loop without bespoke
  scaffolding. Rejected on testability grounds.

The amended design §12 + this plan ship in the same commit
so reviewers can read the intent change against its
authority source.

---

## 1. Scope statement

### In scope (phase 5)

1. **Production defaults module** —
   `app/v2/runtime/_defaults.py`. The clock and id factories
   phase 4 forces every helper to accept now get their
   production wiring here:

   ```python
   PROD_CLOCK = lambda: datetime.now(timezone.utc)
   PROD_RUN_ID_FACTORY = lambda: uuid.uuid4().hex
   PROD_EVENT_ID_FACTORY = lambda: uuid.uuid4().hex
   ```

   Phase 5 is the only module allowed to import `uuid` and
   call `datetime.now()` — the smoke tests on every other
   runtime module already pin that.

2. **APScheduler binding wrapper** —
   `app/v2/runtime/binding.py`. Thin `SchedulerBinding` class
   that wraps `AsyncIOScheduler` with:

   ```python
   class SchedulerBinding:
       async def start(self) -> None: ...
       async def stop(self) -> None: ...
       def register(self, spec: ScheduleSpec) -> None: ...
       def unregister(self, schedule_id: str) -> None: ...
       def reregister(self, spec: ScheduleSpec) -> None: ...
       def list_registered(self) -> list[str]: ...
   ```

   `register` translates the trigger to APScheduler's job
   form and binds the wakeup callable. Phase-4 invariants
   carry forward: APScheduler manages only its
   `apscheduler_jobs` table (design §4.0.3); the v2 runs
   table remains the only source of truth for run state.

3. **Boot sequence module** — `app/v2/runtime/boot.py`. The
   startup hook ordered per design §4.0.5:

   ```python
   async def boot_runtime(
       conn_factory: Callable[[], sqlite3.Connection],
       *,
       worker_count: int = 1,
       claimed_timeout: timedelta = timedelta(minutes=5),
       running_timeout: timedelta = timedelta(minutes=30),
       poll_interval: timedelta = timedelta(seconds=10),
   ) -> "RuntimeHandle": ...

   async def shutdown_runtime(handle: "RuntimeHandle") -> None: ...
   ```

   Per-step:
   1. Open boot-only connection; run `scan_stale_runs(...)`
      with production timeouts.
   2. Log recovery result (count of `RecoveredRun` vs
      `RecoveryError` items). Any `RecoveryError` items
      surface to the caller (the production entry point
      decides whether to abort boot or continue).
   3. Construct `SchedulerBinding`; start it.
   4. Iterate `list_active_schedules(conn)`; register each
      via `binding.register(spec)`.
   5. Construct `worker_count` `Worker` instances; start each.
   6. Return a `RuntimeHandle` carrying the binding + worker
      list for clean shutdown.

4. **Lifecycle hooks** — `app/v2/runtime/lifecycle.py`. Three
   helpers the future authoring path will call on schedule-
   status flips:

   ```python
   def on_schedule_paused(binding, schedule_id) -> None: ...
   def on_schedule_archived(binding, schedule_id) -> None: ...
   def on_schedule_resumed(binding, spec) -> None: ...
   def on_schedule_revised(binding, old_id, new_spec) -> None: ...
   ```

   These translate to APScheduler `pause_job` / `remove_job`
   / `add_job` / re-register. Phase 5 does NOT wire them into
   the storage helpers — that's the authoring-tools phase's
   job. Phase 5 just ships the callables + their tests so
   later phases can call them.

5. **OneOff idempotency contract** — phase 4's wakeup is
   intentionally non-idempotent for OneOff (plan §6.2 says
   "registration layer cleanup owns this"). Phase 5 closes
   the gap via three coordinated mechanisms, NOT a post-fire
   wrapper:

   - **APScheduler natural removal.** `DateTrigger`'s
     `next_run_time` is unset after fire, so APScheduler
     evicts the job from its job store automatically. No
     explicit `unregister` call from phase-5 code.
   - **Register-time DB guard.** `binding.register(spec)`
     for a OneOff checks the `runs` table for any existing
     row with this `schedule_id`. If present, the OneOff
     already fired and is NOT re-registered.
   - **Boot backfill scan.** During `boot_runtime`,
     iterate active OneOff schedules whose `at_iso_datetime`
     is past AND have no existing Run row. Fire `wakeup`
     once per such schedule. Bounded by
     `max_backfill_age` (default 24 h); older orphans are
     logged + skipped.

   The full restart-case matrix is pinned in §3.2.2 below.

6. **Integration test split (round-1 reviewer fix)** —
   end-to-end exercise of the binding/wakeup/worker chain
   split across two test files:

   - **Required fast test** (`test_runtime_e2e_fast.py`,
     default suite): real `SchedulerBinding`, real
     `Worker`, real SQLite DB. Synthesises the "APScheduler
     fired" event by calling `binding._fire_for(
     schedule_id)` directly — no real wall-clock wait. Full
     wakeup → Run → worker → succeeded lifecycle pinned
     inside a 1-second polling budget at 10 ms worker
     ticks.
   - **Optional slow test** (`test_runtime_e2e_slow.py`,
     `@pytest.mark.slow`, excluded from required CI):
     real `AsyncIOScheduler` wall-clock fire of a OneOff
     2 s in the future, 10 s polling budget. Periodic /
     nightly smoke check that APScheduler's event-loop
     integration actually triggers our callback on real
     time.

   Required CI passes if the fast test passes. The slow
   test is documented but not gating.

### Out of scope (phase 5)

- **Any reasoning step execution.** Worker body still empty
  in phase 5 (lights up at phase 12 per renumbered design).
- **Any emit adapter dispatch.** EmitDescriptor runtime
  ingestion lands in phase 12.
- **Cutover from v1 to v2 in production.** `run_bot.py` is
  not touched. Design §12.1 invariant 3 still applies; v1
  scheduler stays the sole production wakeup source through
  phase 8 (boundary shifted with the §12 renumber).
- **Registry cache for channels/sheets/docs** — design §12
  step 6 post-renumber; lands as phase 6.
- **Typed ADK authoring tools** — design §12 step 7
  post-renumber; phase 7 work.
- **Dry-run handshake + boot self-test** — design §12 step 8
  post-renumber; phase 8 work.
- **OneOff end-to-end + emit-only path + v1 cutover** —
  design §12 step 9 post-renumber; phase 9 work.
- **Source loaders** — design §12 step 10 post-renumber;
  phase 10 work.
- **CustomFlow path** — folded into the authoring-tools
  phase (7).
- **APScheduler job store choice** — closed in §9.1:
  SQLAlchemyJobStore per design §4.0.5.

---

## 2. New file paths

```
app/v2/runtime/
  _defaults.py            # production clock + id factories
  cron_guard.py           # extracted from phase-4 wakeup.py;
                          # shared numeric-DOW rejection
  binding.py              # SchedulerBinding(AsyncIOScheduler wrapper)
  boot.py                 # boot_runtime / shutdown_runtime
  lifecycle.py            # pause/archive/resume/revise hooks

tests/v2/
  test_runtime_defaults.py
  test_runtime_cron_guard.py
  test_runtime_binding.py
  test_runtime_boot.py
  test_runtime_lifecycle.py
  test_runtime_e2e_fast.py   # default suite — direct _fire_for
  test_runtime_e2e_slow.py   # @pytest.mark.slow — real AsyncIOScheduler

docs/PHASE_5_PLAN.md         # this file
docs/CONTRACTS_V2_DESIGN.md  # §12 renumber applied in slice 0
```

No edits to phase-3 storage modules expected. One small
phase-4 surface refinement: `app/v2/runtime/wakeup.py`
loses its private `_reject_numeric_dow` (extracted to
`cron_guard.py`) and gains a one-line import. The phase-4
wakeup tests stay green unchanged. This is the only
exception to the "phase 5 builds strictly on top" rule
and ships in slice 1 with an explicit "phase-4 surface
refinement" subject line.

---

## 3. Module APIs (full sketches)

### 3.1 `_defaults.py`

```python
"""Production wiring for the phase-4 runtime injectables."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable


def prod_clock() -> datetime:
    """Return current wall-clock time in UTC."""
    return datetime.now(timezone.utc)


def prod_run_id_factory() -> str:
    """Return a fresh UUID4 hex string for a new Run row."""
    return uuid.uuid4().hex


def prod_event_id_factory() -> str:
    """Return a fresh UUID4 hex string for a new Event row."""
    return uuid.uuid4().hex


__all__ = [
    "prod_clock",
    "prod_event_id_factory",
    "prod_run_id_factory",
]
```

Smoke test pin: this is the ONLY runtime module allowed to
import `uuid` or call `datetime.now()`. Add an inverse test —
every OTHER runtime module's smoke check stays as it is; this
module's smoke check asserts that `uuid` and `datetime.now`
ARE present (so a regression that quietly removes them surfaces
loudly).

### 3.2 `binding.py`

```python
class SchedulerBinding:
    """Wraps one AsyncIOScheduler instance.

    Owns the APScheduler lifecycle + the job-registration ↔
    wakeup-callable plumbing. The v2 ``schedules`` table
    remains the single source of truth for schedule INTENT;
    APScheduler's ``apscheduler_jobs`` table is the
    next-fire-time index that APScheduler manages itself.

    **Job store: SQLAlchemyJobStore + ``misfire_grace_time``
    per design §4.0.5.** APScheduler persists each job's
    next_run_time across restarts so missed-downtime fires
    are recovered. Default ``misfire_grace_time=3600`` (1 h)
    matches the value v1 uses today and the design line at
    §4.0.5 ("up to 1h by default"). The job store URL
    points at the v2-specific SQLite path (see §3.2.1 below)
    so v2's APScheduler state stays separate from v1's
    while both schedulers run in parallel.

    **Trigger translation:**

    - ``OneOffTrigger`` → ``DateTrigger(run_date=
      at_iso_datetime, timezone=ZoneInfo(tz))`` registered
      with ``misfire_grace_time=3600``. APScheduler's
      ``DateTrigger`` naturally removes the job after fire,
      so post-fire cleanup is APScheduler's job — NOT an
      explicit ``unregister`` wrapper. See §3.2.2 below for
      the OneOff lifecycle decision + the boot-side
      idempotency guard that prevents re-registering a
      already-fired OneOff.
    - ``CronTrigger`` → ``CronTrigger.from_crontab(cron,
      timezone=ZoneInfo(tz))`` registered with
      ``misfire_grace_time=3600``. Numeric DOW is rejected
      at register time via the SHARED helper in
      ``app/v2/runtime/cron_guard.py`` (extracted from
      phase-4 ``wakeup.py``; see §3.2.3 below). Reviewer
      round-1 confirmed: APScheduler ACCEPTS numeric DOW
      with Monday=0 semantics, so the rejection MUST happen
      before APScheduler sees the expression. Relying on
      "APScheduler rejects it" is wrong.
    - ``IntervalTrigger`` / ``EventTrigger`` /
      ``ConditionalTrigger`` → ``register`` raises
      ``NotImplementedError`` with the same per-type
      message ``wakeup`` uses.

    **APScheduler callback error handling.** When
    APScheduler fires a job, it invokes a bound method
    ``_fire_for(schedule_id)`` which calls
    ``self._wakeup_callable(...)`` inside its own try /
    except. ANY exception raised by the wakeup callable is
    caught at this boundary, logged via
    ``logger.exception(...)``, and SWALLOWED — propagating
    it would surface as an APScheduler-internal error and
    could pause the scheduler. The job stays registered;
    subsequent fires retry. Pinned by a dedicated test
    (see §5.2 test list).

    Lifecycle methods are documented as async coroutines for
    uniformity with ``Worker.start()`` / ``Worker.stop()``,
    but internally call APScheduler 3.x's SYNC methods
    (``AsyncIOScheduler.start()`` and ``.shutdown()`` both
    sync in the installed API). The coroutine wraps the sync
    call and returns; no blocking work happens.
    """

    def __init__(
        self,
        wakeup_callable: Callable[..., list[str]],
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        clock: Callable[[], datetime] = prod_clock,
        run_id_factory: Callable[[], str] = prod_run_id_factory,
        event_id_factory: Callable[[], str] = prod_event_id_factory,
        jobstore_url: str = "sqlite:///data/scheduler-v2-jobs.db",
        misfire_grace_time: int = 3600,
    ) -> None: ...

    async def start(self) -> None: ...  # wraps sync .start()
    async def stop(self) -> None: ...   # wraps sync .shutdown()

    def register(self, spec: ScheduleSpec) -> None: ...
    def unregister(self, schedule_id: str) -> None: ...
    def reregister(self, spec: ScheduleSpec) -> None: ...
    def list_registered(self) -> list[str]: ...
```

### 3.2.1 v2 jobstore DB path

Design §290 says "Single SQLite database
`data/ori-scheduler.db` (same file)". That's the
post-cutover target. During the v1/v2 parallel period
(through phase 8 post-renumber), v1's APScheduler
ALREADY owns the `apscheduler_jobs` table in
`data/ori-scheduler.db`. Phase 5 introduces a SECOND
APScheduler instance (v2's binding) that must NOT share
the same job store — collisions on job ids would corrupt
v1's running production.

Phase-5 default: separate file
`data/scheduler-v2-jobs.db`. v1 keeps
`data/ori-scheduler.db`; v2's binding uses the new file.
The phase-9 cutover slice (post-renumber, when
`OneOffReminder` fires end-to-end) migrates v2 onto
`data/ori-scheduler.db` or retires that file in favour
of v2's path when v1 is decommissioned.

The v2 BUSINESS schema (the `runs` / `schedules` / `events`
tables from phase 3) lives in a separate file altogether
(`data/scheduler-v2.db` — phase 3's existing convention).
Two v2 files, one for business data and one for
APScheduler's next-fire index. Production wiring passes the
business-data path to `boot_runtime` via its `conn_factory`
argument; the jobstore URL is the binding's separate
concern.

### 3.2.2 OneOff cleanup mechanic (decision)

Phase 4 plan §6.2 left this open. Phase 5 picks
**Path A: rely on APScheduler's natural DateTrigger
removal + DB existing-run guard at register time + boot
backfill scan.**

Why Path A over an explicit `unregister` wrapper:

- APScheduler's `DateTrigger` is already documented to
  remove the job from its job store after fire. Wrapping
  the wakeup with a hand-rolled post-fire unregister
  duplicates that work and risks a race window (job fires →
  our unregister begins → another worker registers the
  same id).
- The wakeup callable already inserts a Run row. If a
  OneOff fires twice (because the user re-registered before
  our wrapper unregistered, or because APScheduler's job
  store outlived the fire), the second wakeup would insert
  a SECOND Run row — exactly the non-idempotency phase 4
  flagged.

Pinning the guarantee comes from two boot-time / register-
time checks instead of post-fire cleanup:

1. **Register-time guard.** Before registering a OneOff,
   `binding.register(spec)` calls
   `_one_off_already_fired(conn, schedule_id)` which checks
   the `runs` table for any row with this schedule_id
   (regardless of status). If any row exists, the OneOff
   already fired and is NOT re-registered. This handles
   the "boot after the OneOff fired and was cleaned up by
   APScheduler" case — the schedule still has
   `status='active'` in the DB but no new run should be
   created.
2. **Boot backfill scan.** During `boot_runtime`, AFTER
   recovery but BEFORE registering forward-looking
   triggers, scan for active OneOff schedules whose
   `at_iso_datetime <= now` and which have NO existing
   Run row. Fire `wakeup` once per such schedule
   immediately (inserts the pending Run + event). This
   handles the "process was down across the OneOff's fire
   time and APScheduler's misfire_grace_time also expired"
   case — without this we'd silently miss the reminder.
   Bounded by `at_iso_datetime > now - max_backfill_age`
   (default 24 h, configurable per
   `boot_runtime(max_backfill_age=...)`); older missed
   OneOffs are logged and skipped rather than firing days
   late.

Restart cases pinned by tests:

- Process down across OneOff fire time + downtime under
  ``misfire_grace_time``: APScheduler's natural misfire
  handling fires the job at boot. Our wakeup runs; pending
  Run inserted; worker picks up; no special code needed.
- Process down across OneOff fire time + downtime BEYOND
  ``misfire_grace_time``: APScheduler drops the job
  silently. Boot backfill scan finds the orphaned active
  OneOff with past `at_iso_datetime` and fires it.
- Process down AFTER OneOff fired but BEFORE the worker
  finished: recovery scan picks up the
  `claimed`/`running` Run; QUEUE_RETRY pushes a new
  pending Run; worker walks it on boot.
- Process restarted while OneOff is still future:
  register-time guard sees no existing Run; registers as
  fresh; APScheduler fires at the configured time.

### 3.2.3 DOW guard extraction (cron_guard.py)

Reviewer round-1 finding: relying on APScheduler to reject
numeric DOW is invalid — APScheduler ACCEPTS it with
Monday=0 semantics, which silently diverges from Unix
cron's Sunday=0. The phase-4 `_reject_numeric_dow` helper
in `app/v2/runtime/wakeup.py` is the chokepoint that
catches this; the binding's `register(cron_spec)` needs
the same check pre-APScheduler so a broken schedule fails
at boot rather than at first fire.

Slice 1 includes the extraction: move
`_reject_numeric_dow` from `wakeup.py` to a new module
`app/v2/runtime/cron_guard.py`; rename to public
`reject_numeric_dow`; both `wakeup.py` and `binding.py`
import from there. The phase-4 wakeup tests stay green
(same behaviour, just imported from a different location).

### 3.3 `boot.py`

```python
@dataclass(frozen=True)
class BackfilledOneOff:
    """A OneOff schedule whose at_iso_datetime was past at
    boot AND had no existing Run row — fired during
    boot backfill (§3.2.2 mechanic 2)."""

    schedule_id: str
    fire_at: datetime
    run_id: str  # the newly inserted pending Run


@dataclass(frozen=True)
class RegistrationError:
    """A schedule that the binding refused to register at
    boot (unsupported trigger type, invalid cron, numeric
    DOW, unknown timezone). The boot continues; other
    schedules still register; the caller can decide whether
    to alert."""

    schedule_id: str
    error_message: str


@dataclass(frozen=True)
class RuntimeHandle:
    binding: SchedulerBinding
    workers: list[Worker]
    recovery_result: list[Union[RecoveredRun, RecoveryError]]
    backfilled_one_offs: list[BackfilledOneOff]
    registration_errors: list[RegistrationError]


class RuntimeBootError(RuntimeError):
    """Raised by ``boot_runtime`` when one of the strict
    abort thresholds trips:

    - ``abort_on_recovery_errors=True`` and any
      ``RecoveryError`` items surfaced.

    Other failures (per-schedule register failures, OneOff
    backfill failures) are tracked in the handle and a
    structured boot-health signal is logged; they do NOT
    raise."""


async def boot_runtime(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    worker_count: int = 1,
    claimed_timeout: timedelta = timedelta(minutes=5),
    running_timeout: timedelta = timedelta(minutes=30),
    poll_interval: timedelta = timedelta(seconds=10),
    claim_batch_size: int = 10,
    max_backfill_age: timedelta = timedelta(hours=24),
    abort_on_recovery_errors: bool = False,
    jobstore_url: str = "sqlite:///data/scheduler-v2-jobs.db",
    misfire_grace_time: int = 3600,
) -> RuntimeHandle: ...


async def shutdown_runtime(handle: RuntimeHandle) -> None: ...
```

Per-step:

1. **Open boot connection.** One short-lived connection
   via `conn_factory` for the synchronous boot work.
2. **Recovery scan.** Call `scan_stale_runs(...)` with
   production timeouts. Capture the full result list. Log
   a one-line structured summary
   (`runtime.boot.recovery: recovered=N errors=M
   schedule_ids=[...]`).
3. **Recovery abort threshold.** If
   `abort_on_recovery_errors=True` AND any `RecoveryError`
   surfaced, raise `RuntimeBootError` with the first
   error's context. Default False (continue, surface
   errors via the handle + log).
4. **Construct + start binding PAUSED.** Construct
   `SchedulerBinding(jobstore_url=..., misfire_grace_time=
   ...)`; `await binding.start(paused=True)`. The paused
   start lets the binding's `start()` initialise the
   scheduler's event loop + load the SQLAlchemy jobstore
   WITHOUT firing any persisted jobs yet. This is the
   round-2 reviewer's race fix: a persisted `DateTrigger`
   for a OneOff whose `at_iso_datetime` passed during
   downtime would otherwise fire CONCURRENTLY with the
   step-5 backfill (both inserting Run rows for the same
   schedule). Pausing during reconciliation closes the
   window.
5. **OneOff boot backfill** (§3.2.2 mechanic 2). For each
   active OneOff schedule with `at_iso_datetime <= now`
   AND no existing Run row AND
   `now - at_iso_datetime <= max_backfill_age`: call
   `wakeup(...)` once. Each successful fire produces a
   `BackfilledOneOff` entry. Older orphans
   (beyond `max_backfill_age`) are logged + skipped
   (entry in `registration_errors` with explicit
   "missed beyond max_backfill_age" reason). Failures
   logged + surfaced via `registration_errors`.
6. **Reconcile jobstore.** After backfill lands, any
   persisted `DateTrigger` in the SQLAlchemy jobstore
   whose schedule now has a Run row in the DB is evicted
   via `binding.unregister(schedule_id)`. This prevents
   the resumed scheduler from firing a redundant wakeup
   for a OneOff we just backfilled.
7. **Register forward-looking schedules.** Iterate
   `list_active_schedules(conn)`. For each spec, call
   `binding.register(spec)` inside a per-spec
   try / except. On failure: log
   (`runtime.boot.register: schedule_id=...
   error="..."`), append a `RegistrationError` entry,
   continue. The OneOff register-time guard (§3.2.2
   mechanic 1) filters out already-fired OneOffs at this
   step too — including ones that landed in steps 2 / 5.
8. **Resume scheduler.** `binding._scheduler.resume()`.
   APScheduler now replays any persisted job whose
   `next_run_time` is within `misfire_grace_time` and
   fires normally on its schedule going forward. Anything
   beyond grace was handled in step 5 (boot backfill).
9. **Worker pool.** Construct `worker_count` `Worker`
   instances; each gets its own `conn_factory` call so
   workers have independent connections. Call
   `await worker.start()` on each.
10. **Close boot connection.** Return `RuntimeHandle`
    carrying binding + workers + recovery_result +
    backfilled_one_offs + registration_errors. The caller
    (production entry, integration test, dev rig) can
    inspect the handle for boot-health signals and decide
    whether to alert.

`shutdown_runtime`:

1. For each worker (in start order): `await worker.stop()`.
2. `await handle.binding.stop()`.
3. Return.

**Boot-health signal contract.** A caller checking
`handle.recovery_result` / `handle.registration_errors` /
`handle.backfilled_one_offs` after boot can detect every
non-fatal anomaly:

- `RecoveryError` items → a stale Run row could not be
  remediated.
- `RegistrationError` items → a schedule could not be
  registered (broken cron, unsupported trigger, missed
  beyond max_backfill_age).
- `BackfilledOneOff` items → at least one missed-downtime
  OneOff fired during boot; observability dashboards may
  want to show this.

Logging is one line per category; structured logging keys
are stable so log-routing rules can be written against
them.

### 3.4 `lifecycle.py`

```python
def on_schedule_paused(
    binding: SchedulerBinding,
    schedule_id: str,
) -> None:
    """Remove the APScheduler job; pending runs stay in the
    DB but cannot fire / be claimed (claim_run's
    schedule-status predicate refuses them, and the binding
    no longer fires the wakeup for this schedule)."""
    binding.unregister(schedule_id)


def on_schedule_archived(
    binding: SchedulerBinding,
    schedule_id: str,
) -> None:
    """Same as paused for phase 5 (no DB-side cleanup of
    pending runs — that's the paused_pending_policy open
    question per PHASE_4_PLAN §12 q5)."""
    binding.unregister(schedule_id)


def on_schedule_resumed(
    binding: SchedulerBinding,
    spec: ScheduleSpec,
) -> None:
    """Re-register the schedule. Caller passes the fresh
    spec (status flipped back to 'active') so register sees
    the post-resume trigger config."""
    binding.register(spec)


def on_schedule_revised(
    binding: SchedulerBinding,
    spec: ScheduleSpec,
) -> None:
    """Trigger / delivery / failure-policy edits land. The
    binding re-registers the job under the same schedule_id
    so APScheduler picks up the new trigger config."""
    binding.reregister(spec)
```

These helpers are pure-function-of-binding-state — they
don't touch the SQLite layer. The future authoring tools
call BOTH the storage helper (`update_schedule_status`) AND
the appropriate lifecycle hook. Phase 5 just ships the hooks
+ their tests so phase-7 authoring code can wire them up.

---

## 4. Slice ordering + commit cadence

Same cadence as phase 4: plan-only commit first (this doc),
then each implementation slice in its own commit. Reviewer
gates each slice; no slice starts until the previous lands
clean.

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan) | `docs/PHASE_5_PLAN.md` + `docs/CONTRACTS_V2_DESIGN.md` §12 renumber + `.v2-current-phase` bump 4→5 + `PHASE_ALLOWLIST[5]` | (none) |
| 1 | `_defaults.py` + `cron_guard.py` (extracted public helper from phase-4 wakeup) + minimal `wakeup.py` edit to import from new location | `test_runtime_defaults.py` + `test_runtime_cron_guard.py` (phase-4 wakeup tests stay green unchanged) |
| 2 | `binding.py` — construction + start/stop only (no register) + APScheduler exception-swallow `_fire_for` skeleton | `test_runtime_binding.py` (lifecycle + exception swallow) |
| 3 | `binding.py` — `register(spec)` for OneOff + Cron + reject of unwired types + numeric-DOW rejection at register time | `test_runtime_binding.py` (registration) |
| 4 | `binding.py` — `unregister`, `reregister`, OneOff register-time guard (`_one_off_already_fired`) | `test_runtime_binding.py` (revisions + guard) |
| 5 | `boot.py` — recovery + binding + backfill + register + workers in documented order | `test_runtime_boot.py` (mocked SchedulerBinding so this test stays fast) |
| 6 | `lifecycle.py` | `test_runtime_lifecycle.py` |
| 7a | binding-wiring fast test via direct `binding._fire_for` invocation (no real wall-clock wait) | `test_runtime_e2e_fast.py` (no marker — runs in default suite) |
| 7b | end-to-end integration test against real `AsyncIOScheduler` (real wall-clock 2 s wait) | `test_runtime_e2e_slow.py` (`@pytest.mark.slow`; excluded from required fast CI) |
| closeout | acceptance + tag `v2-phase-5-complete` (gated on Sergey) | — |

Each slice MUST pin its scope to a single concept. Mixing
"add register + add unregister" into one commit defeats
review.

Slice 1 includes a phase-4 surface refinement (extract
`_reject_numeric_dow` from `wakeup.py` to a new module).
Per the §1 "phase-5 builds on top" rule, this is a small
isolated edit; the phase-4 wakeup tests must stay green
unchanged. The commit message explicitly notes the
extraction.

---

## 5. Test inventory

### 5.1 `test_runtime_defaults.py`

- `prod_clock()` returns a tz-aware datetime in UTC.
- `prod_run_id_factory()` / `prod_event_id_factory()` return
  unique 32-char hex strings (pin `uuid4().hex` shape).
- Two consecutive calls return distinct ids.
- Inverse smoke: the module DOES import `uuid` and the source
  DOES call `datetime.now(timezone.utc)` — pin so a refactor
  that quietly removes them surfaces here. (All OTHER
  runtime modules' smoke checks already pin the OPPOSITE.)

### 5.2 `test_runtime_binding.py`

Lifecycle + construction:

- Construction with all defaults → succeeds; binding starts
  with empty job list.
- Construction with explicit jobstore_url → succeeds.
- `start()` twice → `RuntimeError` (mirrors `Worker`).
- `stop()` before `start()` → no-op.
- `start()` / `stop()` are awaitable coroutines; internally
  they call APScheduler's sync `start()` / `shutdown()` —
  pin the bridge.

Registration:

- `register(one_off_spec)` schedules a `DateTrigger` job
  with `misfire_grace_time=3600`.
- `register(cron_spec)` schedules a cron job with
  `misfire_grace_time=3600`; numeric DOW spec raises
  `ValueError` AT REGISTER TIME (no wait until fire) — pin
  that the rejection comes from `cron_guard`, NOT from
  APScheduler.
- `register(interval_spec)` / event / conditional → raises
  `NotImplementedError` with explicit per-type message.
- `register` twice on the same `schedule_id` → idempotent
  via internal `replace_existing=True`. Pin behaviour
  explicitly so a future flip to "raise on duplicate" is a
  flagged test failure.
- `reregister(spec)` swaps the trigger atomically via
  APScheduler's `reschedule_job(job_id, trigger=...)`
  call — NOT a `remove_job` + `add_job` pair, which would
  open a window where the job briefly does not exist.
  Pin via spying on `reschedule_job`; assert it was called
  exactly once with the new trigger and that
  `list_registered()` always contains the schedule_id
  during the operation.
- `register` then `unregister` → job removed; subsequent
  `list_registered()` does not include it.

OneOff register-time guard:

- `register(one_off_spec)` when the DB already has any Run
  row for that schedule_id → register is a no-op (no job
  added). Pin the §3.2.2 mechanic 1 guarantee directly.
- `register(one_off_spec)` when the DB has no Run row → job
  added normally.

APScheduler callback error swallow:

- Construct a binding whose `wakeup_callable` raises a
  synthetic `Exception` on call. Invoke `binding._fire_for(
  schedule_id)` directly. Pin: exception is caught, logged
  at ERROR via `logger.exception`, NOT re-raised. The job
  stays registered; the scheduler isn't paused.
- Same test but `wakeup_callable` raises
  `KeyboardInterrupt` (a `BaseException`). Pin: this
  PROPAGATES out (same shape as the `worker._run_loop`
  pattern — cancel signals are sacred).

Phase-4 contract carry-forward:

- The wakeup callable still receives `conn`, `schedule_id`,
  `now`, `run_id_factory`, `event_id_factory`. Pin the
  binding passes EXACTLY those keyword args.
- `now` comes from the binding's injected `clock`, not
  from `datetime.now()` — pin via a synthetic clock that
  returns a fixed value, observe the wakeup spy gets that
  value.

### 5.3 `test_runtime_boot.py`

Uses a mocked `SchedulerBinding` so the test is fast (no
real APScheduler). The `binding.py` integration is covered
by the e2e tests in §5.5.

Step order + happy paths:

- Empty DB → boot succeeds; recovery result is empty;
  `binding.start` called; 0 schedules registered; N
  workers started. Handle's `recovery_result` /
  `registration_errors` / `backfilled_one_offs` are all
  empty.
- Active schedules in DB → each registered via
  `binding.register`. Pin order: recovery runs first,
  binding.start second, OneOff backfill third, register
  loop fourth, workers fifth.
- Mixed active + paused + archived → only active schedules
  registered. Paused / archived ones produce no register
  call.

Recovery integration:

- Stale claimed/running runs at boot → recovery scan
  remediates; `handle.recovery_result` contains the
  `RecoveredRun` items.
- `abort_on_recovery_errors=True` + a synthetic
  `RecoveryError` → `boot_runtime` raises
  `RuntimeBootError`. Workers + binding are NOT started.

OneOff backfill (§3.2.2 mechanic 2):

- Active OneOff with past `at_iso_datetime` and no Run row
  → fires during boot; `handle.backfilled_one_offs`
  contains one entry; pending Run row visible in DB.
- Active OneOff with `at_iso_datetime` older than
  `max_backfill_age` → NOT fired;
  `handle.registration_errors` contains an entry with
  reason "missed beyond max_backfill_age".
- Active OneOff with past `at_iso_datetime` but already
  has a Run row → NOT fired; not in
  `backfilled_one_offs`; register-loop step also skips
  it (§3.2.2 mechanic 1).
- OneOff with FUTURE `at_iso_datetime` → not backfilled
  (would be wrong), registered normally in the next step.

Registration errors:

- One schedule with a broken cron (numeric DOW written
  into the DB directly) → register raises; entry added to
  `handle.registration_errors`; OTHER schedules still
  register; workers still start.
- One schedule with an unsupported trigger type → same.
- `worker_count=3` → 3 worker tasks started, each with its
  own conn_factory call.

Shutdown:

- `shutdown_runtime` stops every worker, then stops the
  binding, in that order.
- Double-shutdown is a no-op (no extra `stop` calls on
  workers or binding).

### 5.4 `test_runtime_lifecycle.py`

- `on_schedule_paused(binding, schedule_id)` calls
  `binding.unregister(schedule_id)`.
- `on_schedule_archived(...)` calls `binding.unregister(...)`.
- `on_schedule_resumed(binding, spec)` calls
  `binding.register(spec)`.
- `on_schedule_revised(binding, spec)` calls
  `binding.reregister(spec)`.
- Module exposes ONLY those four public callables (and
  type-only helpers). No "execute" / "fire" / "claim" /
  "reason" / "emit" surfaced.

### 5.5 End-to-end integration — split into fast + slow

Reviewer round-1 raised CI-flake risk on the 5 s real-time
budget. Split into two tests:

**5.5.a `test_runtime_e2e_fast.py` — direct `_fire_for`
invocation (NO real wall-clock wait).**

Default-suite test (NOT marked slow). Uses real
`SchedulerBinding`, real `Worker`, real SQLite DB.
Synthesises the "APScheduler fired the job" event by
calling `binding._fire_for(schedule_id)` directly. No
`binding.start()` call, no `boot_runtime` — this test
exercises the wakeup → Run → worker chain without any
scheduler timing or boot reconciliation.

Deterministic recipe:

1. Seed an active OneOff schedule with
   `at_iso_datetime = synthetic_now` (any tz-aware value
   the test controls; need not be wall-clock-real).
2. Construct `SchedulerBinding` with a clock that returns
   `synthetic_now` and counter-based id factories.
3. Do NOT call `binding.start()` and do NOT call
   `binding.register(spec)` — both are tested in
   `test_runtime_binding.py`. The fast e2e test exercises
   the fire path only.
4. Call `await binding._fire_for(schedule_id)` directly.
   This invokes the wakeup (which inserts pending Run +
   run_created event in the same TX).
5. Construct a `Worker` with the same clock + factories,
   `poll_interval=10 ms`. Call `await worker.tick()` once.
   The single tick walks `pending → claimed → running →
   succeeded` and emits the three worker-side events.
6. Inspect the DB directly: assert four events in order
   (`run_created`, `run_claimed`, `run_started`,
   `run_succeeded`); assert `started_at` / `completed_at`
   populated.
7. No teardown of binding / scheduler needed — neither was
   started.

This proves the wiring: binding → wakeup → DB → worker →
DB → final state. No real-time dependency; no flake risk.

**5.5.b `test_runtime_e2e_slow.py` (`@pytest.mark.slow`,
excluded from required fast CI) — real AsyncIOScheduler
wall-clock wait.**

Slow / integration / non-required CI test. Exercises the
real APScheduler event-loop integration:

- Seed an active OneOff with `at_iso_datetime = now + 2 s`.
- `await boot_runtime(...)` with default timeouts.
- Wait (poll + small sleep) until the run row reaches
  `succeeded` OR a 10-second wall-clock budget elapses.
- Assert lifecycle complete.
- Shut down cleanly.

Marked `@pytest.mark.slow` and excluded from the required
fast CI lane. Runs in nightly / pre-release smoke. The
fast test (5.5.a) is what guarantees PR-time confidence
in the wiring; the slow test is the periodic check that
APScheduler's event-loop integration actually fires our
job on wall-clock time.

---

## 6. CI guard checks

`scripts/check_phase_scope.py` gains `PHASE_ALLOWLIST[5]`
mirroring phase 4's allowlist:

```python
5: {
    "app/v2/",
    "tests/v2/",
    "scripts/check_phase_scope.py",
    "scripts/install_hooks.py",
    ".githooks/v2_phase_guard.sh",
    ".githooks/pre-commit",
    ".github/workflows/v2_phase_guard.yml",
    ".v2-current-phase",
    "docs/PHASE_5_PLAN.md",
    "docs/CONTRACTS_V2_DESIGN.md",
    ".docs_read_marker",
},
```

The v1-paths-forbidden rule (§12.1 invariant 3) carries
forward — phase 5 still cannot touch
`app/contracts/`, `app/tasks.py`, `app/contracts/executor.py`,
`app/scheduler_instance.py`, `data/contracts/`.

Cross-cutting smoke checks added or carried:

- `_defaults.py` is the ONLY runtime module whose smoke
  test asserts uuid / datetime.now ARE imported. Every
  other module's existing smoke test forbids them.
- No public callable in any phase-5 module dispatches to
  reasoning / emit / sub-agents / delegate / transfer.
  Carried from phase 4.
- `binding.py` imports `apscheduler` (allowed); does NOT
  import `httpx` / `requests` / etc.
- `boot.py` imports `binding` + the phase-4 runtime
  modules + the phase-3 storage modules. Smoke tests pin
  no I/O lib imports.

---

## 7. Acceptance criteria for `v2-phase-5-complete`

1. Branch ahead of `v2-phase-4-complete` by N small commits,
   each scoped to one of the slices in §4.
2. `_defaults.py` ships the three production injectables;
   smoke test pins they're importable + behaviourally sane.
3. `cron_guard.py` exposes a public `reject_numeric_dow`;
   `wakeup.py` imports from there; phase-4 wakeup tests
   stay green unchanged.
4. `SchedulerBinding.start()` / `stop()` lifecycle pinned
   (async coroutine wrappers over APScheduler's sync API).
5. `register` translates OneOff + Cron to APScheduler jobs
   with `misfire_grace_time=3600`; raises for unwired
   trigger types; rejects numeric DOW at registration time
   via `cron_guard.reject_numeric_dow`.
6. `unregister` / `reregister` work; `reregister` uses
   APScheduler's `reschedule_job` (atomic — no
   intermediate no-job window). OneOff register-time DB
   guard pinned.
7. `_fire_for` swallows `Exception` (logged via
   `logger.exception`) and propagates `BaseException`.
8. `boot_runtime` runs the documented 10-step sequence
   (recovery → paused binding start → backfill → jobstore
   reconciliation → register → resume → workers); broken
   schedules surface as `RegistrationError` items in the
   handle, not aborts.
9. `RuntimeHandle` carries `recovery_result`,
   `backfilled_one_offs`, `registration_errors`. Structured
   boot-health logging pinned with stable log keys
   (`runtime.boot.recovery` /
   `runtime.boot.register` / `runtime.boot.backfill`).
10. `shutdown_runtime` stops workers before stopping the
    binding; idempotent across double-stop.
11. Lifecycle hooks delegate to the binding; module surface
    exposes ONLY the four documented helpers.
12. **Required CI:** `test_runtime_e2e_fast.py` passes in
    the default suite (no `@pytest.mark.slow`); exercises
    the wakeup → Run → worker → succeeded chain via
    direct `_fire_for` invocation with no real-time
    dependency.
13. **Optional smoke:** `test_runtime_e2e_slow.py` passes
    under `@pytest.mark.slow` against real
    `AsyncIOScheduler` within a 10 s wall-clock budget.
    Not gating on PRs; runs in nightly / pre-release.
14. Phase guard clean against `v2-phase-4-complete`.
15. No v1 scheduler paths touched.
16. `run_bot.py` untouched — v2 still test-rig only.
17. Annotated git tag `v2-phase-5-complete` created and
    pushed (gated on explicit Sergey approval).

---

## 8. Tag annotation

```
v2 phase 5 complete

Production wiring around the phase-4 runtime: AsyncIO
APScheduler binding, boot sequence (recovery + schedule
registration + worker pool), pause/archive/resume lifecycle
hooks, production clock + id factories.

NO production cutover. run_bot.py untouched; v1 scheduler
still owns the production wakeup path until phase 9
(OneOffReminder end-to-end per design §12.1 invariant 3
post-renumber). v2 runtime is now startable in tests / dev
rigs against a temp DB.

Design: docs/CONTRACTS_V2_DESIGN.md §4.0.3, §4.0.5, §12 step 4-5
Plan:   docs/PHASE_5_PLAN.md
```

---

## 9. Open questions

### 9.1 Closed in this revision

1. ~~Design-doc renumber.~~ **CLOSED.** §0 applies the
   amendment in this same commit: design §12 step 5 becomes
   "APScheduler binding + boot sequence"; existing 5–16
   renumber to 6–17.
2. ~~APScheduler job store.~~ **CLOSED.** Design §4.0.5
   explicitly specifies SQLAlchemyJobStore +
   `misfire_grace_time` for the crash-recovery story. The
   earlier draft's MemoryJobStore contradicted that.
   §3.2 now uses SQLAlchemyJobStore against a v2-specific
   path (`data/scheduler-v2-jobs.db` — see §3.2.1) so v2
   and v1 don't share APScheduler state during the
   parallel period.
3. ~~OneOff cleanup mechanic.~~ **CLOSED.** §3.2.2 picks
   Path A: APScheduler's natural DateTrigger removal +
   register-time DB existing-run guard + boot backfill
   scan for missed-downtime OneOffs.
4. ~~DOW guard placement.~~ **CLOSED.** §3.2.3 extracts
   `_reject_numeric_dow` to `app/v2/runtime/cron_guard.py`;
   shared by both `wakeup.py` and `binding.py`. Reviewer
   round-1 finding: "let APScheduler reject" is invalid —
   APScheduler accepts numeric DOW with diverging
   semantics.

### 9.2 Still open

5. **Worker count default.** Plan says 1. Reviewer call.
   Production may want >1 for cross-schedule parallelism
   (single-flight is per-schedule; multiple workers process
   distinct schedules concurrently). 1 keeps the phase-5
   integration tests deterministic; tunable via
   `boot_runtime(worker_count=N)`.

6. **`replace_existing=True` semantics.** Plan default:
   idempotent; `register` and `reregister` collapse for
   callers via APScheduler's
   `add_job(..., replace_existing=True)`. Open question:
   should `register` on a re-add be a no-op silently, or
   should it log a warning? Default = silent for parity
   with APScheduler. Reviewer to confirm at slice 3.

7. **Recovery-error abort threshold.** Right now
   `abort_on_recovery_errors` is binary. Production might
   prefer "abort if > N errors". Not in phase 5 scope; add
   if reviewer wants a threshold instead.

8. **Production deployment integration.** When does
   `boot_runtime` actually get called from `run_bot.py`?
   Phase 5 ships the function but no caller. Phase 9
   cutover (post-renumber) adds the caller. In the
   interim, dev runs invoke `boot_runtime` from a one-off
   CLI script if needed.

9. **Post-cutover jobstore path.** §3.2.1 defaults to
   `data/scheduler-v2-jobs.db` during the parallel period.
   Phase 9 cutover (post-renumber) decides whether to
   migrate to `data/ori-scheduler.db` (the design §4.0.5
   target) or keep v2 on its separate file forever. Not a
   phase-5 call.

---

## 10. Hard rules (carried forward)

These survive across phases unchanged:

1. No push without explicit Sergey approval.
2. No edits to phase-1/2/3/4 plan docs without
   `PHASE_OVERRIDE:` mechanism.
3. No time estimates.
4. Pause after each commit for reviewer.
5. `uv run python …` always.
6. Pre-commit hook needs `.docs_read_marker` —
   `echo "yes" | uv run python scripts/check_docs_read.py`.
7. Commit messages end with
   `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
8. Leave `app/tools/youtube.py` dirty/uncommitted unless
   reviewer flags otherwise.
9. No v1 scheduler edits (carried until phase 9 cutover
   post-renumber).
10. Every runtime helper still takes injected clock + id
    factories; `_defaults.py` is the ONLY module that wires
    them to wall clock + uuid4.
