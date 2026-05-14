# Scheduler v2 — Phase 3 plan

Phase 2 shipped 2026-05-14 (tag `v2-phase-2-complete`). Phase 3
implements design contract §12 step 3:

> Storage layer: SQLite migrations, WAL mode, basic CRUD helpers
> + tests.

WAL + the v001 migration shipped in phase 1
(`app/v2/migrations/runner.py`, `app/v2/ddl/v001_initial.sql`).
Phase 3 builds the typed read/write API on top of that schema.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §4.0 (DDL + invariants)
- `docs/CONTRACTS_V2_DESIGN.md` §4.0.4 (claim / chain /
  idempotency / ledger invariants)
- Phase 1 plan §3 (migration strategy, test-DB-only rule)

---

## 1. Scope statement

### In scope (phase 3)

1. Typed CRUD helpers over the six v2 business tables that
   landed in v001 (`schedules`, `execution_plans`, `runs`,
   `events`, `schedule_state`, `source_snapshots`).
2. A small transaction primitive that lets the caller compose
   "state transition + matching event row in the same
   transaction" without writing raw `BEGIN; ... COMMIT;` plumbing.
3. JSON-column serialisation glue: Pydantic model ↔ JSON string
   so callers pass typed values and the helper takes care of
   the wire shape.
4. A read-only `lookup` / `get` surface that returns Pydantic
   models reconstructed from SQLite rows.
5. CAS primitive for `schedule_state` writes (compare-version,
   set-new-version) per design §4.0.4 ledger transactionality.
6. Event append primitives that enforce append-only semantics
   and the same-TX-as-transition rule.
7. Integration tests against per-test ephemeral SQLite DBs
   (`tmp_path` fixture) — same model as phase 1's migration
   tests.

### Out of scope (phase 3)

- **Any** runtime / worker / wakeup / APScheduler code.
- **Run claim — fully deferred to phase 4.** The single-flight
  predicate per design §4.0.4 + the `pending → claimed`
  transition + every test that exercises claim ownership ALL
  live in phase 4 with the worker. Phase 3's storage surface
  does NOT include `claim_run` and does NOT include any
  single-flight or "exactly-one worker wins" test. The reasoning:
  the moment storage knows about claim semantics it has crossed
  into runtime territory; per design §12 step 4 the claim ships
  with the worker, not the data plane.
- Recovery scan on boot.
- Authoring tools, freeze tool, dry-run handshake.
- Coordinator / sub-agent edits.
- V1 scheduler edits (`app/contracts/`, `app/tasks.py`,
  `app/contracts/executor.py`, `app/scheduler_instance.py`,
  `data/contracts/`).
- Implicit production-DB access. Every storage call takes an
  explicit connection passed in by the caller.

---

## 2. New file paths

```
app/v2/
  storage/
    __init__.py                # re-exports
    connection.py              # connection contract + pragma assertions
    serialization.py           # Pydantic ↔ JSON column glue
    transactions.py            # transaction context manager
    schedules.py               # CRUD on `schedules`
    execution_plans.py         # CRUD on `execution_plans`
    runs.py                    # CRUD on `runs` (incl. claim helper, no loop)
    events.py                  # append-only ledger helper
    schedule_state.py          # CAS + read helpers for `schedule_state`
    source_snapshots.py        # CRUD on `source_snapshots`

tests/v2/
  test_storage_connection.py
  test_storage_serialization.py
  test_storage_transactions.py
  test_storage_schedules.py
  test_storage_execution_plans.py
  test_storage_runs.py
  test_storage_events.py
  test_storage_schedule_state.py
  test_storage_source_snapshots.py

docs/PHASE_3_PLAN.md           # this file
```

Existing files touched (scaffolding only):

- `scripts/check_phase_scope.py` — added `PHASE_ALLOWLIST[3]`
  mirroring phase-2's machinery surface (with PHASE_3_PLAN.md
  replacing PHASE_2_PLAN.md).
- `.v2-current-phase` — bumped from `2` to `3`.

Everything else is additive.

---

## 3. DB connection contract

### 3.1 Caller-passes-connection invariant

Every storage helper takes an explicit `sqlite3.Connection` as
its first positional argument. **No helper opens or holds a
connection itself.** No helper imports
`data/ori-scheduler.db` or any production path. This carries
the phase-1 rule forward: production wiring is phase 4's job.

### 3.2 Connection assertions

A single helper, `assert_connection_ready(conn)`, checks:

- `PRAGMA journal_mode` is `wal` (set by the migration runner).
- `PRAGMA foreign_keys` is on.
- `applied_migrations` table exists and contains `v001_initial`.

Helpers that mutate state call `assert_connection_ready` once
at function entry. The cost is two `PRAGMA` reads + one
`SELECT` — cheap enough to leave on in tests; production
opt-out is a later phase concern.

### 3.3 Connection lifecycle

`autocommit` / `isolation_level=None`. The migration runner
already sets this; tests assert it before exercising CRUD. The
storage layer never calls `conn.commit()` or `conn.rollback()`
outside the explicit transaction primitive (§4).

### 3.4 Threading

SQLite default: one connection per thread. The storage layer
documents the requirement but does not enforce it (Python's
`sqlite3` already raises if you violate it). Phase 4's worker
pool may revisit, but that's not phase 3.

---

## 4. Transaction API

### 4.1 Primitive

```python
@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block inside an explicit BEGIN ... COMMIT.

    On any exception, the runner rolls back and re-raises. On
    a clean exit, it commits.
    """
```

Implementation issues `BEGIN` on enter, `COMMIT` on clean exit,
`ROLLBACK` + re-raise on exception. Nested transactions are
NOT supported in phase 3 — SQLite doesn't have real nested
TX, and a `SAVEPOINT`-based emulation invites bugs. A caller
that needs composition writes the larger block as one
`transaction()`.

### 4.2 Atomic UPDATE-run-status + append-event helper

The design's "ledger transactionality" invariant (§4.0.4 row 4)
requires that a Run row mutation and its matching Event row
land in the same TX. Phase 3 ships a deliberately **neutral**
storage primitive that bundles the two writes atomically:

```python
def update_run_status_and_append_event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    new_status: RunStatus,
    event: Event,
    extra_columns: Optional[dict[str, Any]] = None,
) -> None:
    """Atomically (a) UPDATE runs.status = new_status (plus any
    extra columns, e.g. started_at, completed_at) and (b)
    INSERT the matching event row. Both inside one
    BEGIN ... COMMIT. Raises if the UPDATE affects 0 rows."""
```

**Storage primitive, not a state-machine step.** The helper
performs an unpredicated `UPDATE runs WHERE id = ?` — no
single-flight check, no source-status guard, no claim
ownership. Phase 3 tests must not exercise the
``pending → claimed`` transition or any other claim-shaped
behavior; that's phase 4's job. The atomicity guarantee
belongs in phase 3 because it's a SQLite transaction
property of the data plane; the *policy* of which
transitions are legal lives in phase 4's state machine.

The helper opens its own `transaction()` block internally
unless the caller is already inside one. For phase 3 we keep
the simple rule: caller is NOT inside a transaction when
calling this helper; the helper owns the TX. Nested-aware
behavior is a later concern.

### 4.3 Rollback contract

Any storage-helper exception inside a `transaction()` block
triggers ROLLBACK. The caller is responsible for catching /
handling the exception. The helper never swallows errors
silently — design §13 (the "no silent failure" rule) applies
at the storage boundary too.

---

## 5. CRUD surfaces

Each module exposes a small named-argument-only API. Helpers
return Pydantic models reconstructed from rows; mutating
helpers return the affected primary key (or raise).

### 5.1 `schedules`

```python
insert_schedule(conn, spec: ScheduleSpec) -> str         # returns spec.id
get_schedule(conn, schedule_id: str) -> Optional[ScheduleSpec]
list_active_schedules(conn) -> list[ScheduleSpec]
update_schedule_status(conn, schedule_id: str, status: ScheduleStatus) -> None
```

Insertion enforces the storage row shape: `trigger_json`,
`delivery_json`, `failure_json`, `audit_json` come from
`model_dump_json` of the matching Pydantic sub-objects.
`template_json` is `None` when `spec.template is None`. The
helper does NOT compute or modify the hash — the caller is
expected to have called `spec.with_fresh_hash()` (phase 2's
validator enforces hash present).

### 5.2 `execution_plans`

```python
insert_execution_plan(conn, plan: ExecutionPlan) -> str  # returns plan.hash
get_execution_plan(conn, hash_: str) -> Optional[ExecutionPlan]
```

Plans are immutable; there is no `update_*` helper. INSERT
fails with `IntegrityError` on duplicate hash (correct
behavior — the caller should call `get_execution_plan` first
if they want the existing row).

### 5.3 `runs`

```python
insert_run(conn, run: Run) -> str                        # returns run.id
get_run(conn, run_id: str) -> Optional[Run]
list_pending_due(conn, *, now: datetime, limit: int) -> list[Run]
list_runs_in_chain(conn, root_run_id: str) -> list[Run]  # ordered by attempt
mark_run_status(conn, run_id: str, *, status: RunStatus, **extra) -> None
```

All five helpers are neutral CRUD over the `runs` table:

- `insert_run` writes a row from a Pydantic ``Run``.
- `get_run` returns a Pydantic ``Run`` or ``None``.
- `list_pending_due` orders by ``due_at`` and respects
  ``limit`` — used by the future wakeup callback. It is a
  read-only query; it never mutates and never claims.
- `list_runs_in_chain` returns the retry chain in attempt
  order via ``WHERE root_run_id = ?``.
- `mark_run_status` runs an unpredicated
  ``UPDATE runs SET status = ?, <extra...> WHERE id = ?``.
  No source-status guard, no single-flight predicate. The
  call site is responsible for any prior consistency check.

**Deferred to phase 4 (do not implement in phase 3):**

- `claim_run` — single-flight ``UPDATE`` with
  ``WHERE status='pending' AND NOT EXISTS(...)``. The SQL
  per design §4.0.4 lives with the worker that calls it on
  a loop.
- Recovery-scan helpers that promote ``claimed`` / ``running``
  back to ``pending`` after a timeout.

Phase-3 tests for ``runs`` must NOT exercise pending → claimed
or any single-flight scenario. Any test that walks a status
chain restricts itself to neutral round-trips (insert + get;
mark + get).

### 5.4 `events`

```python
append_event(conn, event: Event) -> str                  # returns event.id
list_events_for_run(conn, run_id: str) -> list[Event]
list_events_for_schedule(conn, schedule_id: str, *, kind: Optional[EventKind] = None, limit: int = 200) -> list[Event]
get_last_emit_succeeded(conn, idempotency_key: str) -> Optional[Event]
```

Append-only — no `update_event` / `delete_event`. The helper
ENFORCES this by simply not exposing the operations; the SQL
boundary doesn't have a separate guard. `append_event` accepts
events with `run_id is None` (schedule-level events) per the
phase-1 model.

### 5.5 `schedule_state`

```python
get_state(conn, *, schedule_id: str, key: str) -> Optional[ScheduleState]
set_state_cas(
    conn,
    *,
    schedule_id: str,
    key: str,
    new_value: Any,
    expected_version: int,
    written_by_run: Optional[str],
    now: datetime,
) -> bool
```

`set_state_cas` is the only writer. It runs:

```sql
UPDATE schedule_state
SET value_json = ?, version = version + 1, written_at = ?, written_by_run = ?
WHERE schedule_id = ? AND key = ? AND version = ?
```

(With a separate `INSERT OR IGNORE` fall-back for first-time
writes when `expected_version == 0`.) Returns True iff exactly
one row was affected. The caller's responsibility is to read
the current state, decide what new state to compute, then
attempt CAS with the current version; on False, retry.

### 5.6 `source_snapshots`

```python
insert_snapshot(conn, meta: SourceSnapshotMetadata) -> tuple[str, str]
get_snapshot(conn, *, run_id: str, source_id: str) -> Optional[SourceSnapshotMetadata]
list_snapshots_by_hash(conn, content_hash: str) -> list[SourceSnapshotMetadata]
```

Returns `(run_id, source_id)` tuple as the composite primary
key.

---

## 6. JSON serialization

### 6.1 Strategy

Phase-1 DDL stores all complex shapes as `TEXT` columns
serialised as JSON (`trigger_json`, `delivery_json`,
`payload_json`, `value_json`, `body_json`). Phase 3 introduces
a single pair of helpers:

```python
def encode_json(model: BaseModel | Any) -> str
def decode_json(raw: str, target_type: type[T]) -> T
```

`encode_json` is `model.model_dump_json(by_alias=True)` for
Pydantic models, `json.dumps(value, sort_keys=True,
separators=(",", ":"), default=_iso_default)` for plain Python
values (used for `schedule_state.value_json` and
`events.payload_json` which are freeform).

`decode_json` calls `target_type.model_validate_json(raw)` for
Pydantic targets and `json.loads(raw)` for plain types.

### 6.2 Datetime handling

Python `datetime` ↔ ISO 8601 string. The helper enforces
UTC-aware datetimes on the way in (raises on naive) and parses
ISO strings on the way out. Matches the v2 model rule
(see `EmitOutputContract` etc.).

### 6.3 Sort order + deterministic encoding

`sort_keys=True` + `separators=(",", ":")` so equivalent
content produces byte-identical strings — important for hash
recomputation in tests + for any future content-addressed
caching.

---

## 7. CAS primitive details

See §5.5. The CAS semantics:

- First write (no prior row): `version = 1`, written_by_run
  optional, success unconditional.
- Subsequent write: caller passes the version they read; the
  UPDATE only mutates if the DB version still equals what the
  caller saw. On no-match, return False — caller retries with
  fresh read.

CAS does NOT loop internally. That's deliberate: the caller
knows the right policy for their use case (retry now, retry
later, fall through, abort). The helper is the primitive; the
policy is the runtime's job.

The bookkeeping helper writes `written_at = now` and
`written_by_run = run_id` (or null for author-time seeds).
Phase-1 DDL has the FK constraint on `written_by_run`; the
storage layer doesn't add a redundant existence check.

---

## 8. Event append rules

### 8.1 Append-only

`append_event` is the only writer; no update / delete helper
exists. The schema's INSERT-only convention is matched by the
API.

### 8.2 Same-TX-as-transition

When the caller is recording a Run state transition, they call
`transition_run_and_emit_event` (§4.2) which wraps both
operations in a single TX. The storage layer surfaces both
helpers (`append_event` for schedule-level + standalone
events; `transition_run_and_emit_event` for paired
transitions).

### 8.3 No silent failure

Every helper either succeeds, returns a sentinel indicating
the operation didn't happen (e.g. CAS False, claim returns
False), or raises. None of them swallow exceptions. Design
§13 ("nothing fails silently") at the storage boundary.

### 8.4 Idempotency lookup

`get_last_emit_succeeded(conn, idempotency_key)` is the
ledger-side dedup check. It queries:

```sql
SELECT * FROM events
WHERE kind = 'emit_succeeded'
  AND json_extract(payload_json, '$.idempotency_key') = ?
ORDER BY ts DESC LIMIT 1
```

Note: this assumes the runtime puts the idempotency key in
the event payload under the literal key `idempotency_key`.
Phase 3 documents this convention but does NOT enforce it at
the schema level (no CHECK on json_extract — that would
require a generated column and complicate migrations).
Phase-3 tests assert the helper finds the row when written
with the expected payload and finds nothing otherwise.

---

## 9. Test inventory

Each storage module ships with a matching test file. All tests
run against per-test ephemeral DBs via the `tmp_path` fixture +
the phase-1 migration runner.

### 9.1 `test_storage_connection.py`

- `assert_connection_ready` passes on a fresh migrated DB.
- Fails when `journal_mode != wal`.
- Fails when `foreign_keys` is off.
- Fails when `applied_migrations` is absent (manual DB).

### 9.2 `test_storage_serialization.py`

- Pydantic round-trip (encode → decode → equal).
- Plain-value round-trip (dict, list, scalar).
- Naive datetime rejected on encode.
- ISO string with `+00:00` decodes to UTC datetime.
- Sort-order stability: equivalent dicts produce identical
  strings.

### 9.3 `test_storage_transactions.py`

- Clean exit → commit.
- Exception → rollback + re-raise.
- Partial mutation in a rolled-back TX leaves DB untouched.
- `update_run_status_and_append_event` atomic-on-failure: SQL
  error on the event INSERT rolls back the run UPDATE.
- The helper raises when the target run id does not exist
  (the run UPDATE affects 0 rows).
- Test inputs cover a generic non-claim transition (e.g.
  ``running → succeeded``). The pending → claimed shape is
  explicitly NOT tested in phase 3 — that's phase 4's
  state-machine territory.

### 9.4 `test_storage_schedules.py`

- Insert + get round-trip.
- `list_active_schedules` only returns `status='active'`.
- `update_schedule_status` flips and persists.
- FK enforcement: insert a schedule with bogus
  `execution_plan_hash` against an empty `execution_plans` —
  succeeds because the FK is comment-only at the schema level
  (per the §4.0.2 resync). Documented.
- CHECK enforcement: insert with `status='enabled'` raises.

### 9.5 `test_storage_execution_plans.py`

- Insert + get round-trip.
- Duplicate hash raises `IntegrityError`.
- Plans are immutable — no `update_*` exists.

### 9.6 `test_storage_runs.py`

- Insert + get round-trip.
- `list_pending_due` orders by `due_at` and respects `limit`.
- `list_pending_due` excludes non-pending statuses.
- `list_runs_in_chain` returns the chain in attempt order.
- `mark_run_status` writes the new status + any extra columns
  (e.g. `started_at`, `completed_at`, `error`).
- `mark_run_status` is unpredicated: it overwrites the row
  regardless of the prior status. Tests pin this neutrality
  by walking ``running → succeeded`` and asserting no
  source-status filtering is silently applied.

**Explicitly forbidden in phase 3 tests:** the
``pending → claimed`` transition, any single-flight scenario,
any "two workers race" simulation. Those land in phase 4 with
the worker that introduces claim semantics.

### 9.7 `test_storage_events.py`

- Append + list round-trip.
- `list_events_for_run` returns chronological order.
- `list_events_for_schedule` filters by kind when supplied.
- `get_last_emit_succeeded` returns the latest matching
  payload, None when missing.

### 9.8 `test_storage_schedule_state.py`

- First write at `expected_version=0` inserts with version 1.
- CAS with stale version returns False, does not mutate.
- CAS with current version returns True, bumps version.
- `get_state` returns None when absent, ScheduleState when
  present.
- `written_by_run` accepts None (author-time seed) and a real
  run id; FK rejects a ghost run id.

### 9.9 `test_storage_source_snapshots.py`

- Insert + get round-trip.
- Composite PK enforcement (duplicate `(run_id, source_id)`
  raises).
- `list_snapshots_by_hash` returns all matching rows.

### 9.10 Cross-cutting smoke checks

- **No production-DB access**: monkey-patch `sqlite3.connect`
  and assert the storage modules never call it implicitly.
- **No worker-style methods**: storage modules expose no
  callable named `run`, `loop`, `start`, `worker`, `dispatch`,
  `execute`, `invoke`, `send`.
- **No I/O imports**: forbid httpx / requests / slack_sdk /
  googleapiclient / telegram / smtplib / subprocess in
  `app.v2.storage.*` (same pattern as phase 2 registry +
  validation smoke tests).

---

## 10. CI guard checks

`PHASE_ALLOWLIST[3]` mirrors phase 2's machinery surface, with
`docs/PHASE_3_PLAN.md` replacing `docs/PHASE_2_PLAN.md`. The
phase 2 plan doc is intentionally OUT of the phase-3 allowlist
— if a phase-2 doc fix is needed during phase 3, use the
`PHASE_OVERRIDE:` mechanism per design §6.4.

`scripts/check_phase_scope.py` itself stays in the allowlist
so future phase widenings can land via the same path that
brought phase 3 in.

---

## 11. Commit ordering

Test-first invariant from design §11.3: every code commit
ships with matching tests in the same commit. Each module-pair
(code + test) is a small slice. Suggested order (subject to
reviewer + Sergey override):

| # | scope | files |
|---|---|---|
| 0 | this plan + phase bump + allowlist widening | `docs/PHASE_3_PLAN.md`, `.v2-current-phase`, `scripts/check_phase_scope.py` |
| 1 | connection contract + serialization helpers + tests | `app/v2/storage/connection.py`, `serialization.py`, `__init__.py`; matching tests |
| 2 | transactions + the composite transition helper + tests | `app/v2/storage/transactions.py` + test |
| 3 | schedules CRUD + tests | `app/v2/storage/schedules.py` + test |
| 4 | execution_plans CRUD + tests | `app/v2/storage/execution_plans.py` + test |
| 5 | runs CRUD (neutral; claim helper deferred to phase 4) + tests | `app/v2/storage/runs.py` + test |
| 6 | events append + tests | `app/v2/storage/events.py` + test |
| 7 | schedule_state CAS + tests | `app/v2/storage/schedule_state.py` + test |
| 8 | source_snapshots CRUD + tests | `app/v2/storage/source_snapshots.py` + test |
| close | acceptance criteria + tag | `docs/PHASE_3_PLAN.md` row update |

Each slice pauses for reviewer. Sequence may compress if
slices land cleanly.

---

## 12. Acceptance criteria for phase 3 completion

Phase 3 is complete when ALL of the following hold:

1. All files in §2 exist.
2. `uv run python -m pytest tests/v2/ --tb=short -q` passes
   with zero failures.
3. `uv run python -m pytest tests/` (full suite) still passes.
4. `uv run python scripts/check_phase_scope.py --diff v2-phase-2-complete`
   reports no violations.
5. Every helper signature in §5 is implemented and tested.
6. The CAS test exercises both success and stale-version paths.
7. The update-status-and-append-event helper test confirms
   atomic rollback when either side fails, AND uses a neutral
   transition (no `pending → claimed`).
7a. No phase-3 test exercises `pending → claimed`,
    single-flight, or any claim ownership scenario; those are
    phase 4.
8. The smoke-test invariants (§9.10) pass: no production DB
   access, no worker-style callables, no I/O imports.
9. CI workflow `.github/workflows/v2_phase_guard.yml` passes
   against the phase-3 diff.
10. Annotated git tag `v2-phase-3-complete` created and
    pushed.

---

## 13. Phase-3 tag annotation

```
v2 phase 3 complete

Adds the storage layer over the v001 SQLite schema:
typed CRUD helpers for schedules, execution_plans, runs, events,
schedule_state, source_snapshots; transaction primitive +
atomic update-run-status-and-append-event helper; CAS for
schedule_state; JSON ↔ Pydantic serialization glue.

No runtime worker. No APScheduler wakeup. No Run claim.
Claim semantics + single-flight + recovery scan land in
phase 4 with the worker. v1 paths untouched. Production DB
never accessed implicitly — every helper takes an explicit
connection.

Design: docs/CONTRACTS_V2_DESIGN.md §4.0.4, §12 step 3
Plan:   docs/PHASE_3_PLAN.md
```

---

## 14. Open questions

None at plan-doc-time. Filed here as they surface.
