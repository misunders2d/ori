# Scheduler v2 — Phase 8 plan

Phase 7 shipped 2026-05-15 (tag `v2-phase-7-complete` on origin
at `6986b15`). Phase 8 introduces the **dry-run handshake +
freeze + commit + `schedule_created` event** triplet per
design `docs/CONTRACTS_V2_DESIGN.md` §5.6 and §12 step 8
(post-renumber). This phase closes the commit verb phase 7
deferred (round-2 reviewer L95 / Q5).

This phase is deliberately scoped to **reminder-only specs**
(OneOffTrigger without ExecutionPlan). The `mocked_inputs`
and `real` dry-run modes from design §5.6 depend on
ExecutionPlan + source loaders (phases 10 / 12); phase 8
returns a documented `mode_not_implemented_in_phase_8`
validation failure for those modes. Boot self-test (design
§6.7) lands in a later phase too — the admin-alert path
depends on production wiring v2 has not cut over to.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §5.5 (chokepoint),
  §5.6 (dry-run handshake + modes), §6.7 (boot self-test —
  deferred), §12 step 8, §12.1 invariants 2 + 3.
- `docs/PHASE_7_PLAN.md` §3.5 (compile deferred-commit
  pointer), §9.1.a item 6 (Q5 commit → phase 8).

---

## 0. Design-doc amendment

None expected. §5.6 already specifies the three modes + the
60s handshake window + the freeze recording shape. If
reviewer rounds surface a gap, apply the amendment as a
plan-revision commit and update this §0 in that commit.

---

## 1. Scope statement

### In scope (phase 8)

1. **Dry-run handshake state** —
   `app/v2/authoring/handshake.py`. File-backed JSON at
   `tmp/v2_handshakes/<session_id>/<draft_id>.json`. Same
   atomic mkstemp+rename + path-traversal fence (slug regex
   + `Path.resolve().is_relative_to(base)`) as the phase-7
   `DraftStore`. Pydantic `HandshakeRecord` carrying
   `draft_id`, `session_id`, `body_hash`, `mode`,
   `as_of_datetime` (optional, tz-aware UTC), `recorded_at`
   (tz-aware UTC), `expires_at` (tz-aware UTC, recorded_at
   + 60s per design §5.6).

2. **`schedule_dry_run`** — `app/v2/authoring/dry_run.py`.
   Validates the draft → spec → runs the §5.5 chokepoint;
   on success writes a fresh `HandshakeRecord`. Three modes:

   - `validate_only` — fully implemented. Pydantic +
     `validate_schedule_spec(spec)` with no
     `execution_plans` / `registries` kwargs (reminder-only
     flow carries forward from phase 7).
   - `mocked_inputs` — returns
     `ToolResponse.validation_failed` with code
     `mode_not_implemented_in_phase_8` naming the
     ExecutionPlan dependency (phase 10/12 work). Plan
     scaffolding for the mode constant lands here so phase
     10/12 can flip the body to the real implementation in
     a focused commit.
   - `real` — same shape as `mocked_inputs`.

   Optional `as_of_datetime` keyword is accepted for all
   modes but ignored in `validate_only` (no time-sensitive
   loader logic in reminder-only flow). Tz-aware UTC pin
   carries forward.

3. **`schedule_freeze`** — `app/v2/authoring/freeze.py`.
   Re-validates the draft → spec, then runs the
   trigger-type gate, then checks the freshest
   `HandshakeRecord`:
   - **Trigger-type gate (round-1 reviewer L87 fix +
     Q4):** if `spec.trigger.type != "one_off"` →
     `ToolResponse.validation_failed` code
     `non_oneoff_trigger_blocked_until_real_mode` BEFORE
     the handshake check. Phase 8 is OneOff-only;
     `mocked_inputs` / `real` dry-run modes are stubs, and
     design §5.6 says cron / interval require `real`.
     Refusing the freeze outright is the only sound
     behaviour — `validate_only` alone cannot certify a
     cron schedule. Phase 10 / 12 lift this gate when
     `real` ships.
   - Missing handshake → `ToolResponse.validation_failed`
     code `dry_run_required`.
   - Expired handshake (`now > expires_at`) →
     `ToolResponse.validation_failed` code
     `dry_run_expired` with the elapsed seconds.
   - Hash drift (`spec.compute_hash() != handshake.body_hash`)
     → `ToolResponse.validation_failed` code
     `body_hash_drift`. The author mutated the draft after
     the dry-run; they must re-run dry_run before
     freezing.
   - Success: return `ToolResponse.ok(spec=spec.model_dump())`.
     The freeze itself does NOT touch the DB; it just
     verifies the handshake remains valid for the commit
     verb. The handshake stays on disk.

4. **`schedule_draft_commit`** — `app/v2/authoring/commit.py`.
   The commit verb deferred from phase 7.

   **Pre-flight gates** (same as `schedule_freeze`):
   - Trigger-type gate: non-OneOff → `validation_failed`
     code `non_oneoff_trigger_blocked_until_real_mode`
     (round-1 reviewer L87 / Q4). Pre-flight; runs before
     any DB I/O.
   - Handshake presence / expiry / hash drift checks
     (same codes as freeze).

   **Atomic in one TX** (only reached after gates pass):
   - `insert_schedule(conn, spec)` — phase-3 storage
     helper. Surfaces ``sqlite3.IntegrityError`` on a
     duplicate id; the tool catches it and maps to
     ``validation_failed`` code `duplicate_schedule_id`
     (round-1 reviewer Q8 confirmed shape).
   - `append_event(conn, Event(kind=SCHEDULE_CREATED, ...))`.
     The event id comes from the DI'd `event_id_factory`;
     `ts` from `clock()`; `payload` is exactly
     ``{"hash": spec.hash, "template": <name or None>}``
     (round-1 reviewer Q6 confirmed shape).
   - Commit the TX.

   **Post-commit cleanup (round-1 reviewer L99 + L99
   model):** best-effort with WARNING log on failure. After
   the DB TX commits successfully:
   - Attempt `store.delete(session_id, draft_id)`. Any
     ``Exception`` is caught, logged at WARNING level on
     the module's logger
     (``app.v2.authoring.commit``) with the schedule id +
     the offending path, and DOES NOT raise; the schedule
     is already on disk and the EventLedger entry is the
     source of truth.
   - Same for `handshake_store.delete(session_id,
     draft_id)`. Independent try/except so a draft-delete
     failure does not prevent the handshake cleanup
     attempt.

   The post-commit cleanup is explicitly **best-effort**:
   the response is still ``ToolResponse.ok(schedule_id,
   spec)`` even when one or both file deletes fail. A
   regression test pins the WARNING log emission so a
   future change that silences the failure surfaces.

   Failure of the pre-flight gates OR of the DB TX:
   - Rolls back the DB TX.
   - Leaves the draft + handshake files in place for retry
     / forensics.
   - Returns the `ToolResponse.validation_failed` shape
     produced by the failing gate (or a
     `validation_failed(duplicate_schedule_id)` for the
     storage path).

   Successful response: ``ToolResponse.ok(schedule_id=spec.id,
   spec=spec.model_dump())``.

5. **Toolset extension** —
   `app/v2/toolsets/authoring.py` gains three new
   `FunctionTool` instances + three `ToolDescriptor`
   records per the §4 tag matrix:
   - `schedule_dry_run`: `filesystem_read` +
     `filesystem_write` (reads draft; writes handshake).
   - `schedule_freeze`: `filesystem_read` (reads draft +
     handshake; no DB write, no file write).
   - `schedule_draft_commit`: `db_write` +
     `filesystem_write` (DB insert + deletes draft +
     handshake on success).

### Out of scope (phase 8)

- **`mocked_inputs` / `real` dry-run modes.** Depend on
  ExecutionPlan body + source loaders. Phase 10 / 12 ship
  the real implementations; phase 8 returns the
  `mode_not_implemented_in_phase_8` validation failure so
  the LLM gets a clean error shape.
- **Boot self-test (design §6.7).** The admin-alert
  self-test fires via direct httpx + Slack adapter
  registration; phase-9 cutover wires that. Phase 8 keeps
  `boot_runtime` untouched.
- **`CoordinatorAgent` / `run_bot.py` mounting.** Phase 9
  cutover does this. The new tools land on the
  `AuthoringToolset` but the toolset stays unmounted.
- **`schedule_attach_execution_plan` + `plan_draft_*`** —
  the ExecutionPlan authoring path lands at phase 10 / 12.
- **v1 paths / v1 scheduler.** Invariant 3 still in
  force; cutover at phase 9.
- **`ChannelRef` / `SheetRef` Pydantic enum binding.**
  Phase 7's resolver-time validation stays the binding
  for phase 8 too; tightening to validated enums on the
  field type is post-cutover work.

---

## 2. New file paths

```
app/v2/authoring/handshake.py
app/v2/authoring/dry_run.py
app/v2/authoring/freeze.py
app/v2/authoring/commit.py

tests/v2/test_authoring_handshake.py
tests/v2/test_authoring_dry_run.py
tests/v2/test_authoring_freeze.py
tests/v2/test_authoring_commit.py
```

Existing files touched:
- `app/v2/authoring/__init__.py` — exports the three new
  tools + `DryRunMode` + `HandshakeStore` +
  `HandshakeRecord` + `DEFAULT_HANDSHAKE_BASE`.
- `app/v2/toolsets/authoring.py` — three new
  `FunctionTool` wraps + three `ToolDescriptor` entries.
- `.v2-current-phase` — bump 7 → 8.
- `scripts/check_phase_scope.py` — `PHASE_ALLOWLIST[8]`.
- `docs/PHASE_8_PLAN.md` — this file.

---

## 3. Module APIs

### 3.1 `handshake.py`

```python
class DryRunMode(str, Enum):
    VALIDATE_ONLY = "validate_only"
    MOCKED_INPUTS = "mocked_inputs"
    REAL = "real"


_HANDSHAKE_WINDOW_SECONDS = 60  # design §5.6
DEFAULT_HANDSHAKE_BASE = pathlib.Path("tmp/v2_handshakes")


class HandshakeRecord(BaseModel):
    """Persisted dry-run handshake state. Freeze + commit
    gate on this record's freshness + hash."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    session_id: str
    body_hash: str
    mode: DryRunMode
    as_of_datetime: Optional[datetime] = None
    recorded_at: datetime
    expires_at: datetime

    @field_validator("recorded_at", "expires_at", "as_of_datetime")
    @classmethod
    def _utc_only(cls, v): ...  # naive + non-UTC rejected
        # Same shape as phase-6 _require_utc; None allowed
        # for as_of_datetime.

    def is_expired(self, *, now: datetime) -> bool:
        return now > self.expires_at


class HandshakeStore:
    """File-backed handshake persistence under
    ``tmp/v2_handshakes/<session_id>/<draft_id>.json``. Same
    slug + Path.resolve() fence as DraftStore. Atomic
    mkstemp+rename writes."""

    def __init__(self, *, base: Optional[pathlib.Path] = None) -> None: ...
    def read(self, session_id: str, draft_id: str) -> HandshakeRecord: ...
    def write(self, session_id: str, record: HandshakeRecord) -> None: ...
    def delete(self, session_id: str, draft_id: str) -> None: ...
```

### 3.2 `dry_run.py`

```python
async def schedule_dry_run(
    draft_id: str,
    mode: DryRunMode,
    *,
    session_id: str,
    store: DraftStore,
    handshake_store: HandshakeStore,
    clock: Callable[[], datetime],
    as_of_datetime: Optional[datetime] = None,
) -> ToolResponse:
    """Validate the draft and record a fresh handshake.

    Phase 8 implements ``validate_only`` fully. The two
    other modes return ``ToolResponse.validation_failed``
    with code ``mode_not_implemented_in_phase_8`` naming
    the ExecutionPlan dependency.

    Workflow (validate_only):
    1. ``draft = store.read(session_id, draft_id)``;
       FileNotFoundError → ``ToolResponse.not_found``.
    2. ``missing = draft.missing_required_fields()``;
       non-empty → ``ToolResponse.not_ready``.
    3. ``spec = draft.to_spec(clock=clock)`` (naive clock
       → validation_failed via to_spec_failed).
    4. ``result = validate_schedule_spec(spec)``; issues →
       ``ToolResponse.validation_failed``.
    5. Build a ``HandshakeRecord``:
       - body_hash = spec.hash (the canonical hash from
         with_fresh_hash; same value compile + freeze +
         commit will recompute).
       - mode = ``DryRunMode.VALIDATE_ONLY``.
       - as_of_datetime = caller arg (accepted but ignored
         in validate_only; future modes use it).
       - recorded_at = clock() (UTC-normalised).
       - expires_at = recorded_at + 60s.
    6. ``handshake_store.write(session_id, record)``.
    7. Return ``ToolResponse.ok(spec=spec.model_dump(),
       message=f"dry-run handshake recorded; freeze within
       {window}s")``.
    """
```

### 3.3 `freeze.py`

```python
async def schedule_freeze(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
    handshake_store: HandshakeStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Verify the dry-run handshake is fresh + matches the
    draft, then return the canonical spec for the commit
    verb. The freeze does NOT touch the v2 DB and does NOT
    delete the handshake."""
```

### 3.4 `commit.py`

```python
async def schedule_draft_commit(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
    handshake_store: HandshakeStore,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Land the draft as a row in the v2 schedules table
    AND append a schedule_created EventLedger event in the
    same transaction.

    Gates on the freeze shape (same handshake checks as
    schedule_freeze). On success:
    - Inserts the schedule.
    - Appends a schedule_created event with
      ``payload={"hash": spec.hash, "template": <name|None>}``.
    - Commits.
    - Deletes the draft file.
    - Deletes the handshake file.

    On any failure the DB TX rolls back AND the draft +
    handshake files stay in place so the author can fix
    + retry. Returns ``ToolResponse.ok(schedule_id=spec.id,
    spec=spec.model_dump())``.
    """
```

---

## 4. Slice ordering + commit cadence

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan) | `docs/PHASE_8_PLAN.md` + `.v2-current-phase` 7 → 8 + `PHASE_ALLOWLIST[8]` | — |
| 1 | `handshake.py` — `DryRunMode`, `HandshakeRecord`, `HandshakeStore` | `test_authoring_handshake.py` |
| 2 | `dry_run.py` — validate_only path + handshake recording + mode-not-implemented branches | `test_authoring_dry_run.py` |
| 3 | `freeze.py` — handshake verification + hash-drift gate | `test_authoring_freeze.py` |
| 4 | `commit.py` — atomic insert+event TX + draft/handshake cleanup | `test_authoring_commit.py` |
| 5 | `__init__.py` + toolset extension (3 FunctionTools + 3 ToolDescriptors) | `test_authoring_toolset.py` updates |
| closeout | acceptance + tag `v2-phase-8-complete` (gated on codex pass) | — |

ToolDescriptor tag matrix additions (slice 5):

| Tool | Tags | Rationale |
|---|---|---|
| `schedule_dry_run` | `filesystem_read` + `filesystem_write` | reads draft; writes handshake file. |
| `schedule_freeze` | `filesystem_read` | reads draft + handshake; no mutation. |
| `schedule_draft_commit` | `db_write` + `filesystem_write` | DB insert + draft / handshake delete. |

---

## 5. Test inventory

### 5.1 `test_authoring_handshake.py`

- `DryRunMode` enum has the three documented values.
- `HandshakeRecord` rejects naive + non-UTC datetimes on
  every datetime field; UTC accepted + round-trips.
- `HandshakeRecord.is_expired` returns False at boundary,
  True past `expires_at`.
- `HandshakeStore` round-trip per session/draft pair.
- `HandshakeStore.read` missing file → `FileNotFoundError`.
- `HandshakeStore.delete` idempotent.
- Path-traversal fence: `../`, `..\\`, absolute, empty,
  oversize, symlink-escape (mirror DraftStore pins).
- Atomic write: simulated mid-rename crash preserves
  previous record + leaves a `.tmp.<id>` artifact.
- Concurrent same-pid writes do not collide (mkstemp pin).

### 5.2 `test_authoring_dry_run.py`

- `validate_only` happy path: draft completes, validation
  passes → ok response carrying spec; handshake record
  written; `expires_at == recorded_at + 60s`; `body_hash ==
  spec.hash`; `mode == VALIDATE_ONLY`.
- `validate_only` on incomplete draft → `not_ready`; no
  handshake written.
- `validate_only` on validation-failing spec (cron without
  plan) → `validation_failed`; no handshake written.
- Missing draft → `not_found`.
- `mocked_inputs` mode → `validation_failed` with code
  `mode_not_implemented_in_phase_8`; no handshake.
- `real` mode → same shape as `mocked_inputs`.
- Naive clock → `to_spec_failed` validation_failed; no
  handshake.
- `as_of_datetime` accepted in `validate_only` but stored
  verbatim on the record (no behaviour change).
- `validate_schedule_spec` called with zero kwargs (Q6
  carry-forward pin).

### 5.3 `test_authoring_freeze.py`

- Fresh handshake matching current OneOff draft → ok with
  spec.
- **Cron-trigger draft (round-1 reviewer L87 / Q4 fix):**
  even with a fresh matching handshake → `validation_failed`
  with code `non_oneoff_trigger_blocked_until_real_mode`.
  The gate runs BEFORE the handshake check; pin by ALSO
  asserting the response with no handshake at all surfaces
  the same code (not `dry_run_required`).
- Interval-trigger draft (if reachable from the
  ScheduleSpecDraft model) → same code as cron.
- No handshake → `validation_failed` with code
  `dry_run_required` (OneOff path only).
- Expired handshake → `validation_failed` with code
  `dry_run_expired`; message includes elapsed seconds.
- Hash drift (draft mutated after dry-run) →
  `validation_failed` with code `body_hash_drift`.
- Missing draft → `not_found`.
- Incomplete draft → `not_ready`.
- Freeze does NOT touch the v2 DB; pin via patching
  `insert_schedule` and asserting zero calls.
- Freeze does NOT delete the draft file or the handshake
  file; pin via post-call read.

### 5.4 `test_authoring_commit.py`

- Happy path: complete OneOff draft + fresh handshake +
  matching hash → `ok(schedule_id, spec)`; row visible in
  `schedules` table; `schedule_created` event visible in
  `events` table with the documented payload; draft file
  removed; handshake file removed.
- **Cron-trigger draft (round-1 reviewer L87 / Q4 fix):**
  even with a fresh matching handshake →
  `validation_failed` code
  `non_oneoff_trigger_blocked_until_real_mode`. Pre-flight
  gate runs BEFORE the DB I/O; pin via patching
  `insert_schedule` + `append_event` and asserting zero
  calls. Draft + handshake unchanged.
- No handshake → `validation_failed(dry_run_required)`;
  no row inserted; draft + handshake unchanged.
- Expired handshake → `validation_failed(dry_run_expired)`;
  no row inserted.
- Hash drift → `validation_failed(body_hash_drift)`; no
  row inserted.
- Missing draft → `not_found`.
- Validation-failing spec (somehow re-broken after
  handshake) → `validation_failed`; no row, no event.
- Transaction rollback on event-insert failure:
  monkey-patch `append_event` to raise after
  `insert_schedule`; pin schedules row reverted, no event
  inserted, draft + handshake stay on disk for retry.
- Duplicate id: insert into a DB that already has the
  schedule → `sqlite3.IntegrityError` → tool surfaces as
  `validation_failed` with code `duplicate_schedule_id`;
  draft + handshake stay on disk.
- `schedule_created` event payload contains exactly the
  documented keys: `hash` matches spec.hash; `template` is
  either the template name or None for CustomFlow specs
  (round-1 reviewer Q6).

**Post-commit cleanup (round-1 reviewer L99 fix):**

- Draft-delete failure post-commit: monkey-patch
  `DraftStore.delete` to raise after `insert_schedule`
  and `append_event` succeed. Pin:
  - Schedule row + event row both present (DB TX
    committed).
  - Response is still `ToolResponse.ok` (best-effort
    cleanup).
  - `caplog` records exactly one WARNING entry on
    logger `app.v2.authoring.commit` naming the
    schedule id + the offending path.
  - Handshake delete still attempted (handshake gone /
    or its own WARNING if it also fails — see next).
- Handshake-delete failure post-commit: same shape,
  WARNING on the same logger; response stays ok.
- Both deletes failing in sequence: TWO WARNING entries;
  response stays ok; schedule + event present.
- Inverse pin: clean commit emits ZERO WARNING records
  (the best-effort branch only logs on failure).

### 5.5 `test_authoring_toolset.py` updates

- `get_tools` now returns 17 `FunctionTool` instances
  (14 phase-7 + 3 phase-8).
- New names in the expected set:
  `schedule_dry_run`, `schedule_freeze`,
  `schedule_draft_commit`.
- Tag matrix extended with the three new tools.
- `register_descriptors` covers 17 entries; no
  duplication.

---

## 6. CI guard checks

`scripts/check_phase_scope.py` gains:

```python
8: {
    "app/v2/",
    "tests/v2/",
    "scripts/check_phase_scope.py",
    "scripts/install_hooks.py",
    ".githooks/v2_phase_guard.sh",
    ".githooks/pre-commit",
    ".github/workflows/v2_phase_guard.yml",
    ".v2-current-phase",
    "docs/PHASE_8_PLAN.md",
    "docs/CONTRACTS_V2_DESIGN.md",
    ".docs_read_marker",
},
```

The v1-paths-forbidden rule (§12.1 invariant 3) carries
forward — phase 8 cannot touch `app/contracts/`,
`app/tasks.py`, `app/contracts/executor.py`,
`app/scheduler_instance.py`, `data/contracts/`. Cutover
remains at phase 9.

Cross-cutting smoke checks:

- Phase-8 modules import no I/O libs at module load
  (`slack_sdk` / `googleapiclient` / `httpx` /
  `requests` / `urllib3` / `aiohttp` / `smtplib` /
  `subprocess`). Same shape as phase-7 authoring.
- Phase-8 modules do NOT import
  `app.v2.runtime._defaults` at module load. Clock + id
  factories stay DI.
- No phase-8 module mounts on any agent. AST pin: no
  imports of `app.agent` / `run_bot`.
- `_defaults.py` remains the ONLY runtime / authoring
  module whose smoke test asserts `uuid` /
  `datetime.now` imports.

---

## 7. Acceptance criteria for `v2-phase-8-complete`

1. Branch ahead of `v2-phase-7-complete` by N small commits,
   each scoped to one of the slices in §4.
2. `HandshakeRecord` rejects naive + non-UTC datetimes on
   every datetime field; `is_expired` boundary semantics
   pinned.
3. `HandshakeStore` round-trips, missing-file
   FileNotFoundError, delete idempotent, atomic mkstemp+
   rename, same path-traversal fence as DraftStore (pin
   all six fence cases).
4. `schedule_dry_run` validate_only happy path writes a
   fresh handshake with `expires_at == recorded_at + 60s`
   and `body_hash == spec.hash`.
5. `schedule_dry_run` `mocked_inputs` and `real` return
   `validation_failed(mode_not_implemented_in_phase_8)`.
6. `schedule_freeze` enforces (in order) the trigger-type
   gate, handshake presence, freshness, and matching hash;
   surfaces `non_oneoff_trigger_blocked_until_real_mode` /
   `dry_run_required` / `dry_run_expired` /
   `body_hash_drift` codes; does not mutate DB or files.
7. `schedule_draft_commit` runs the same trigger-type +
   handshake gates AS PRE-FLIGHT, then `insert_schedule`
   AND appends a `schedule_created` event in one TX;
   post-commit best-effort cleanup deletes draft +
   handshake (WARNING log on each delete failure; response
   stays `ok`); pre-commit gate failures + DB rollback
   leave both files in place.
8. Cron / interval drafts are rejected outright at freeze
   AND commit (round-1 reviewer L87 / Q4 — phase 8 is
   OneOff-only until `real` dry-run lands).
9. `schedule_created` event payload contains exactly
   `{"hash": spec.hash, "template": <name or None>}`
   (round-1 reviewer Q6).
10. Post-commit cleanup is best-effort: draft + handshake
    delete failures emit a single WARNING-level log on
    `app.v2.authoring.commit` per failed delete; the
    `ToolResponse` stays `ok` because the EventLedger is
    the source of truth (round-1 reviewer L99 fix). Test
    pin: monkeypatch the deletes to raise; assert log +
    response shape.
11. AuthoringToolset.get_tools returns 17 FunctionTool
    instances; descriptor registry covers all 17.
12. Phase guard `--diff v2-phase-7-complete` clean.
13. No v1 paths touched. `run_bot.py` / `boot_runtime` /
    `CoordinatorAgent` untouched.
14. No `datetime.now()` / `uuid.uuid4()` outside
    `_defaults.py`. AST pin on every phase-8 module.
15. Full v2 test suite passes (existing 1574 + phase-8
    adds); no regressions.
16. Annotated git tag `v2-phase-8-complete` created and
    pushed (workflow pre-approved per phase 5/6/7 pattern).

---

## 8. Tag annotation

```
v2 phase 8 complete

Dry-run handshake + freeze + commit + schedule_created event.
schedule_dry_run records a 60-second handshake binding spec
hash + mode + recorded_at; schedule_freeze verifies the
handshake is fresh + matching before letting the commit verb
proceed; schedule_draft_commit lands the schedule row AND
appends a schedule_created event in one TX, then deletes the
draft + handshake files on success. Failures roll back the DB
TX and leave both files in place for retry.

Phase 8 ships the validate_only dry-run mode in full; the
mocked_inputs and real modes return mode_not_implemented_in_
phase_8 with a hint at the ExecutionPlan dependency. Boot
self-test (design §6.7) stays deferred; admin-alert wiring
lands with the phase-9 cutover.

Freeze + commit are OneOff-only in phase 8: cron / interval
drafts are refused outright with code
non_oneoff_trigger_blocked_until_real_mode. Design §5.6
requires `real` mode for cron schedules; phase 10 / 12 lift
the gate alongside the `real` body.

Post-commit cleanup is best-effort: draft + handshake delete
failures log WARNING and the response stays `ok` (EventLedger
is the source of truth).

NO production agent mounting. CoordinatorAgent untouched;
boot_runtime untouched; no binding wiring. ExecutionPlan
authoring deferred to phases 10 / 12. v1 still owns
production wakeup until phase 9 cutover.

Design: docs/CONTRACTS_V2_DESIGN.md §5.5, §5.6, §11.4,
        §12 step 8
Plan:   docs/PHASE_8_PLAN.md
```

---

## 9. Open questions

### 9.1 Closed in this revision

1. ~~Commit verb scope.~~ **CLOSED** (carried from
   phase-7 Q5): commit lands here, alongside the freeze
   gate and the `schedule_created` event triplet.
2. ~~Boot self-test scope.~~ **CLOSED**: deferred (admin-
   alert path depends on phase-9 production wiring).
3. ~~`mocked_inputs` / `real` modes.~~ **CLOSED**:
   stubbed with explicit error code so phase 10 / 12 can
   flip the body in a focused commit.

### 9.1.a Closed in round-2 reviewer

4. ~~Cron-freeze permissiveness gap.~~ **CLOSED** (L87 /
   Q4): `schedule_freeze` AND `schedule_draft_commit` now
   refuse non-OneOff triggers outright with code
   `non_oneoff_trigger_blocked_until_real_mode`. Phase
   10 / 12 lift the gate when `real` dry-run ships.
5. ~~Handshake file retention.~~ **CLOSED** (Q5): keep
   for a later phase. Phase 8 ships no retention sweep.
6. ~~`schedule_created` payload schema.~~ **CLOSED**
   (Q6): exactly `{"hash": spec.hash, "template":
   <name|None>}`. No `owner` / `authored_at` / full body
   in phase 8; a later phase may extend via the per-kind
   payload model.
7. ~~`as_of_datetime` storage on `validate_only`.~~
   **CLOSED** (Q7): stored on the handshake record
   verbatim; phase-8 behaviour ignores it. Future modes
   consume it.
8. ~~Duplicate-id commit response shape.~~ **CLOSED**
   (Q8): `validation_failed(duplicate_schedule_id)` is
   the right shape.
9. ~~Post-commit cleanup gap.~~ **CLOSED** (L99): the
   commit verb does best-effort cleanup with WARNING log
   on failure; response stays `ok` because the
   EventLedger is the source of truth. Regression test
   pins the WARNING emission.

### 9.2 Still open

None — round-2 reviewer closed every prior open item.
New items will populate here if reviewer rounds 3+
surface gaps.

---

## 10. Hard rules (carried forward)

Same as phase 7 plan §10. Restated for self-containment:

1. No push without explicit reviewer / Sergey approval.
2. No edits to phase-1 through phase-7 plan docs without
   a `PHASE_OVERRIDE:` mechanism in the commit message.
3. No time estimates.
4. Pause after each commit for reviewer.
5. `uv run python …` always.
6. Pre-commit hook needs `.docs_read_marker` —
   `echo "yes" | uv run python scripts/check_docs_read.py`.
7. Commit messages end with
   `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
8. Leave `app/tools/youtube.py` dirty/uncommitted unless
   reviewer flags otherwise; same for the three diag
   scripts at the repo root.
9. No v1 scheduler edits (carried until phase 9 cutover).
10. Every runtime / authoring helper takes injected clock
    + id factories; `_defaults.py` is the ONLY module that
    wires them to wall clock + uuid4.
11. **OneOff-only freeze / commit until phase 10 / 12 ships
    `real` dry-run mode** (round-1 reviewer L87 / Q4): cron
    / interval drafts are refused outright at freeze AND
    commit. Phase 12 lifts the gate alongside the `real`
    dry-run body.
