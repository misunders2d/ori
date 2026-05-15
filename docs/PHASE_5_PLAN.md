# Scheduler v2 — Phase 5 plan

Phase 4 shipped 2026-05-15 (tag `v2-phase-4-complete`). Phase 5
introduces the production-wiring layer around the phase-4
runtime: APScheduler construction + lifecycle, schedule
registration, boot sequence (recovery scan + worker pool
start), and pause/archive/resume hooks.

This phase deliberately stops short of cutting over from v1.
The cutover to v2 for production schedules happens at phase 8
(`OneOffReminder` end-to-end per design §12.1 invariant 3).
Phase 5 makes the v2 runtime **startable** for tests / dev
runs, hooks the schedule-status lifecycle into APScheduler,
and leaves `run_bot.py` untouched.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §4.0.3 (APScheduler-as-wakeup-
  only scope), §4.0.5 (crash / restart recovery), §12 step 4
  + step 5.
- `docs/PHASE_4_PLAN.md` §6.1 (Worker constructor),
  §6.2 (wakeup), §6.3 (APScheduler binding deferral default),
  §12 open questions 3 + 5.

---

## 0. Design-doc drift notice (open question for §13)

Design `docs/CONTRACTS_V2_DESIGN.md` §12 step 5 currently
reads **"Registry cache for channels/sheets/docs"**. That
step is NOT what this plan implements — phase 5 here is
APScheduler binding + production wiring around the phase-4
runtime.

Resolution proposed (gated on Sergey approval to edit the
locked design doc):

- Insert a NEW step **"APScheduler binding + boot sequence"**
  in §12 between current step 4 and step 5.
- Renumber the existing steps 5 onward by +1.

The runtime sequencing argument: design §12 step 4 explicitly
defers the binding ("APScheduler-as-wakeup-only... wakeup
callback that inserts pending Runs"); §12.1 invariant 2 then
holds the binding back until later. Phase 4 plan §6.3 picked
"defer to phase 5" as the default. The new step formalises
that deferral in the design contract itself.

If reviewer / Sergey rejects the renumber, the alternatives:

A. Rename this plan to **"Phase 4 closeout (binding)"** and
   stay numerically inside phase 4. Then the existing
   `v2-phase-4-complete` tag becomes the pre-binding cut and
   a new tag (`v2-phase-4-bound` or similar) caps the
   closeout. Plan §11 covers either.
B. Skip APScheduler binding entirely until phase 8 cutover.
   Phase 5 = Registry cache as design says. Phase 8 then
   bundles binding + cutover in one slice. Rejected by
   default because it leaves v2's runtime untestable
   end-to-end for three phases; phase 6 / 7 work would have
   no way to exercise the wakeup loop without bespoke test
   scaffolding.

Default plan: option (renumber). Sergey to confirm in plan
review.

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

5. **OneOff dedup mechanism** — phase 4's wakeup is
   intentionally non-idempotent for OneOff (plan §6.2 says
   "registration layer cleanup owns this"). Phase 5 lands the
   cleanup: after a OneOff fires, the binding unregisters
   the APScheduler job. Either by `add_job(..., misfire_grace_time=...,
   replace_existing=True)` with a self-removing wrapper, or by
   the `register` helper attaching a tiny post-fire hook that
   calls `binding.unregister(schedule_id)`. The exact mechanic
   is an open question (§12 below).

6. **Integration test** — end-to-end exercise of the boot
   sequence against a temp DB:
   - Seed an active OneOff schedule whose `at_iso_datetime`
     is a few seconds in the future.
   - Boot the runtime.
   - Wait for the worker to process the run.
   - Assert the run row reaches `succeeded` with all four
     events (`run_created`, `run_claimed`, `run_started`,
     `run_succeeded`) in the ledger.
   - Shut the runtime down cleanly.
   - This test uses REAL APScheduler — not a mock — so the
     binding's wiring against `AsyncIOScheduler` is exercised
     end-to-end. The test allows a small wall-clock budget
     (~5 s) and is marked `@pytest.mark.slow` for selective
     skipping in fast-feedback runs.

### Out of scope (phase 5)

- **Any reasoning step execution.** Worker body still empty
  in phase 5 (lights up at phase 11).
- **Any emit adapter dispatch.** EmitDescriptor runtime
  ingestion lands in phase 11.
- **Cutover from v1 to v2 in production.** `run_bot.py` is
  not touched. Design §12.1 invariant 3 still applies; v1
  scheduler stays the sole production wakeup source through
  phase 7.
- **Registry cache for channels/sheets/docs** — this was
  design §12 step 5; with the renumber proposed in §0 it
  becomes step 6 and lands as phase 6.
- **Typed ADK authoring tools** — design §12 step 6 (becomes
  step 7); phase 6 / 7 work.
- **Source loaders** — phase 9.
- **CustomFlow path** — folded into the authoring-tools
  phase.
- **APScheduler job store choice (SQLAlchemy vs Memory)** —
  see §12 open question 2.

---

## 2. New file paths

```
app/v2/runtime/
  _defaults.py            # production clock + id factories
  binding.py              # SchedulerBinding(AsyncIOScheduler wrapper)
  boot.py                 # boot_runtime / shutdown_runtime
  lifecycle.py            # pause/archive/resume/revise hooks

tests/v2/
  test_runtime_defaults.py
  test_runtime_binding.py
  test_runtime_boot.py
  test_runtime_lifecycle.py
  test_runtime_e2e.py     # @pytest.mark.slow integration test

docs/PHASE_5_PLAN.md      # this file
```

No edits to phase-3 storage modules expected. No edits to any
phase-4 runtime module expected — phase 5 builds STRICTLY on
top of the phase-4 surface. If a phase-4 module needs a small
tweak (e.g. a public method that turns out underspecified),
the change ships in its OWN small phase-5 commit with an
explicit "phase-4 surface refinement" subject line.

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
    """Thin wrapper over AsyncIOScheduler.

    Owns the lifecycle of one APScheduler instance and the
    job-registration ↔ wakeup-callable plumbing. The schedule
    table in the v2 SQLite DB remains the single source of
    truth for schedule state; APScheduler's own
    apscheduler_jobs table is treated as a derived index
    (re-buildable from the schedules table on every boot).

    Phase-5 default: MemoryJobStore (no SQLAlchemy dep). On
    every boot the binding is reconstructed from the active
    schedules read out of the v2 DB. This is the simplest
    contract; SQLAlchemy job store may be revisited if
    re-registration overhead becomes a problem.
    """

    def __init__(
        self,
        wakeup_callable: Callable[..., list[str]],
        conn_factory: Callable[[], sqlite3.Connection],
        *,
        clock: Callable[[], datetime] = prod_clock,
        run_id_factory: Callable[[], str] = prod_run_id_factory,
        event_id_factory: Callable[[], str] = prod_event_id_factory,
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def register(self, spec: ScheduleSpec) -> None: ...
    def unregister(self, schedule_id: str) -> None: ...
    def reregister(self, spec: ScheduleSpec) -> None: ...
    def list_registered(self) -> list[str]: ...
```

When APScheduler fires a job, it calls a bound callable
(`_fire_for(schedule_id)`) that internally calls
`self._wakeup_callable(conn, schedule_id=..., now=clock(),
run_id_factory=..., event_id_factory=...)`. The wakeup
function inserts the Run + run_created event; the worker pool
(launched separately by `boot_runtime`) picks the run up on
its next tick.

Trigger translation:

- `OneOffTrigger` → APScheduler `DateTrigger(run_date=
  at_iso_datetime, timezone=ZoneInfo(tz))`. After fire, the
  binding's post-fire hook calls `self.unregister(
  schedule_id)`. This closes the OneOff non-idempotency
  noted in PHASE_4_PLAN §6.2.
- `CronTrigger` → APScheduler `CronTrigger.from_crontab(cron,
  timezone=ZoneInfo(tz))`. Numeric DOW is rejected via the
  same `_reject_numeric_dow` guard already in
  `app/v2/runtime/wakeup.py` (extract to a public helper so
  the binding can call it pre-register, or keep it in wakeup
  and rely on the wakeup-time rejection — see §12 open
  question 4).
- `IntervalTrigger` / `EventTrigger` / `ConditionalTrigger`
  → `register` raises `NotImplementedError` with the same
  per-type message the wakeup function uses.

### 3.3 `boot.py`

```python
@dataclass(frozen=True)
class RuntimeHandle:
    binding: SchedulerBinding
    workers: list[Worker]
    recovery_result: list[Union[RecoveredRun, RecoveryError]]


async def boot_runtime(
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    worker_count: int = 1,
    claimed_timeout: timedelta = timedelta(minutes=5),
    running_timeout: timedelta = timedelta(minutes=30),
    poll_interval: timedelta = timedelta(seconds=10),
    claim_batch_size: int = 10,
    abort_on_recovery_errors: bool = False,
) -> RuntimeHandle: ...


async def shutdown_runtime(handle: RuntimeHandle) -> None: ...
```

Per-step (matches §1 item 3):

1. Open a one-shot connection via `conn_factory`. Call
   `scan_stale_runs(conn, now=prod_clock(), claimed_timeout=
   ..., running_timeout=..., run_id_factory=
   prod_run_id_factory, event_id_factory=
   prod_event_id_factory)`. Log the result.
2. If `abort_on_recovery_errors` is True and any
   `RecoveryError` items surfaced, raise `RuntimeBootError`
   with the first error's context. Default False (log + carry
   on).
3. Construct `SchedulerBinding`; `await binding.start()`.
4. `for spec in list_active_schedules(conn): binding.register(
   spec)`. Catch per-spec exceptions (e.g. an unsupported
   trigger type or an invalid cron); log and continue —
   refusing to register a single broken schedule must not
   abort the whole boot.
5. Construct `worker_count` `Worker` instances. Each gets its
   own `conn_factory` call so workers have independent
   connections (matches phase-4 plan §12 q4 long-lived
   default). Call `await worker.start()` on each.
6. Close the boot-only connection. Return `RuntimeHandle`.

`shutdown_runtime`:

1. For each worker: `await worker.stop()`.
2. `await handle.binding.stop()`.
3. Return.

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
| 0 (plan) | `docs/PHASE_5_PLAN.md` + `.v2-current-phase` bump 4→5 + `PHASE_ALLOWLIST[5]` | (none) |
| 1 | `_defaults.py` | `test_runtime_defaults.py` |
| 2 | `binding.py` — construction + start/stop only (no register) | `test_runtime_binding.py` (lifecycle only) |
| 3 | `binding.py` — `register(spec)` for OneOff + Cron + reject of unwired types | `test_runtime_binding.py` (registration) |
| 4 | `binding.py` — `unregister`, `reregister`, OneOff post-fire cleanup | `test_runtime_binding.py` (revisions) |
| 5 | `boot.py` | `test_runtime_boot.py` (mocked SchedulerBinding so this test stays fast) |
| 6 | `lifecycle.py` | `test_runtime_lifecycle.py` |
| 7 | end-to-end integration test against real `AsyncIOScheduler` | `test_runtime_e2e.py` (`@pytest.mark.slow`) |
| closeout | acceptance + tag `v2-phase-5-complete` (gated on Sergey) | — |

Each slice MUST pin its scope to a single concept. Mixing
"add register + add unregister" into one commit defeats
review.

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

- Construction with all defaults → succeeds; binding starts
  with empty job list.
- `start()` twice → `RuntimeError` (mirrors `Worker`).
- `stop()` before `start()` → no-op.
- `register(one_off_spec)` schedules a `DateTrigger` job.
- `register(cron_spec)` schedules a cron job; numeric DOW
  spec raises `ValueError` at register time (no wait until
  fire).
- `register(interval_spec)` / event / conditional → raises
  `NotImplementedError` with explicit message.
- `register` then `unregister` → job removed; subsequent
  `list_registered()` does not include it.
- `register` twice on the same `schedule_id` → idempotent
  via internal `replace_existing=True`, OR raises a clear
  error. Pin whichever the implementation chooses.
- `reregister(spec)` swaps the trigger atomically (no
  intermediate "no-job" window observable from outside).
- OneOff post-fire cleanup: when APScheduler fires the
  OneOff, the wakeup runs AND the binding unregisters the
  job. Pin via spy + a synthetic in-process clock that
  triggers the job immediately.
- Phase-4 contract carry-forward: the wakeup callable still
  takes `conn`, `schedule_id`, `now`, `run_id_factory`,
  `event_id_factory`. Pin the binding passes EXACTLY those.

### 5.3 `test_runtime_boot.py`

Uses a mocked `SchedulerBinding` so the test is fast (no
real APScheduler). The `binding.py` integration is covered
by the e2e test in §5.5.

- Empty DB → boot succeeds; recovery result is empty;
  binding.start called; 0 schedules registered; N workers
  started.
- Active schedules in DB → each registered.
- Mixed active + paused → only active schedules registered.
- One schedule with a broken cron (e.g. someone wrote
  numeric DOW into the DB directly) → boot logs the
  per-spec error and continues; binding still has the other
  schedules; workers still start.
- Stale claimed/running runs at boot → recovery scan
  remediates; recovery_result on the returned handle
  contains the `RecoveredRun` items.
- `abort_on_recovery_errors=True` + a synthetic
  `RecoveryError` → `boot_runtime` raises `RuntimeBootError`.
- `worker_count=3` → 3 worker tasks started.
- `shutdown_runtime` stops every worker, then stops the
  binding, in that order.

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

### 5.5 `test_runtime_e2e.py` (`@pytest.mark.slow`)

The integration test. Real `AsyncIOScheduler`, real SQLite
DB, real `Worker`.

- Seed an active `OneOffReminder`-shape schedule (no
  ExecutionPlan, OneOff trigger 2 seconds in the future).
- `await boot_runtime(...)` with default timeouts.
- Wait (poll + small sleep) until the run row reaches
  `succeeded` OR a 5-second wall-clock budget elapses.
- Assert the run walked: status=succeeded, four events in
  order (`run_created` → `run_claimed` → `run_started` →
  `run_succeeded`), `started_at` and `completed_at`
  populated.
- `await shutdown_runtime(handle)`.

This test is the single load-bearing proof that phase 5's
binding wires phase 4's pieces correctly under a real
event loop. It's marked `@pytest.mark.slow` so fast feedback
runs (`pytest -m "not slow"`) skip it.

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
3. `SchedulerBinding.start()` / `stop()` lifecycle pinned.
4. `register` translates OneOff + Cron to APScheduler jobs;
   raises for unwired trigger types; rejects numeric DOW
   at registration time.
5. `unregister` / `reregister` work; OneOff post-fire
   cleanup pinned.
6. `boot_runtime` runs recovery + registers active schedules
   + starts workers in the documented order; broken schedules
   are skipped with logging, not aborts.
7. `shutdown_runtime` stops workers before stopping the
   binding; idempotent across double-stop.
8. Lifecycle hooks delegate to the binding; module surface
   exposes ONLY the four documented helpers.
9. End-to-end integration test passes against real
   `AsyncIOScheduler` within a 5-second budget.
10. Phase guard clean against `v2-phase-4-complete`.
11. No v1 scheduler paths touched.
12. `run_bot.py` untouched — v2 still test-rig only.
13. Annotated git tag `v2-phase-5-complete` created and
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
still owns the production wakeup path until phase 8
(OneOffReminder end-to-end per design §12.1 invariant 3).
v2 runtime is now startable in tests / dev rigs against a
temp DB.

Design: docs/CONTRACTS_V2_DESIGN.md §4.0.3, §4.0.5, §12 step 4-5
Plan:   docs/PHASE_5_PLAN.md
```

---

## 9. Open questions

1. **Design-doc renumber.** §0 above proposes inserting an
   APScheduler-binding step at §12 position 5 and shifting
   subsequent steps. Sergey to approve the §12 edit or
   choose alternative A / B in §0.

2. **APScheduler job store: MemoryJobStore vs
   SQLAlchemyJobStore.** Phase 5 default is in-memory: every
   boot reads `list_active_schedules` and re-registers.
   Simplest contract; never out of sync with the v2 DB.
   SQLAlchemyJobStore would persist APScheduler's view across
   restarts but introduces a second source of truth for
   schedule existence (apscheduler_jobs vs schedules) that
   would have to be reconciled on every boot anyway. Not
   worth the dependency cost in phase 5; reviewer can call
   for a switch later if profiling shows re-registration is
   slow on large catalogs.

3. **Worker count default.** Plan says 1. Reviewer call.
   Production may want >1 for cross-schedule parallelism
   (single-flight is per-schedule; multiple workers process
   distinct schedules concurrently). 1 keeps the phase-5
   integration test deterministic; tunable via
   `boot_runtime(worker_count=N)`.

4. **DOW guard placement.** Phase 4's
   `_reject_numeric_dow` lives in `wakeup.py`. The binding
   wants to reject at `register` time (failing fast at boot
   for a broken schedule rather than at first fire). Two
   options:
   - Extract `_reject_numeric_dow` to a shared module
     (e.g. `app/v2/runtime/cron_guard.py`) and call from
     both sites.
   - Keep it private in `wakeup.py`; `binding.register`
     constructs the cron trigger and lets APScheduler reject
     numerics OR re-parses the cron string with the same
     digit-check inline.
   Reviewer call.

5. **`replace_existing=True` semantics.** `register` on a
   schedule_id that's already registered — idempotent or
   error? APScheduler's `add_job(..., replace_existing=True)`
   gives idempotency. Plan default: idempotent;
   `reregister` and `register` collapse for callers. Tests
   pin the chosen behaviour.

6. **OneOff cleanup mechanic.** Two paths:
   - APScheduler's `DateTrigger` naturally fires once and
     APScheduler removes the job from its store. The
     binding's responsibility shrinks to "don't re-register
     a OneOff that already fired" — handled by checking the
     DB for existing pending/claimed/running/succeeded runs
     of that schedule before registering.
   - Explicit post-fire callback that calls
     `self.unregister(schedule_id)` from inside the wakeup
     wrapper.
   First path is simpler; second is more explicit. Reviewer
   call after slice 4.

7. **Recovery-error abort threshold.** Right now
   `abort_on_recovery_errors` is binary. Production might
   prefer "abort if > N errors". Not in phase 5 scope; add
   if reviewer wants a threshold instead.

8. **Production deployment integration.** When does
   `boot_runtime` actually get called from `run_bot.py`?
   Phase 5 ships the function but no caller. Phase 8 cutover
   adds the caller. In the interim, dev runs invoke
   `boot_runtime` from a one-off CLI script if needed.

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
9. No v1 scheduler edits (carried until phase 8 cutover).
10. Every runtime helper still takes injected clock + id
    factories; `_defaults.py` is the ONLY module that wires
    them to wall clock + uuid4.
