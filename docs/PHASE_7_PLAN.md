# Scheduler v2 — Phase 7 plan

Phase 6 shipped 2026-05-15 (tag `v2-phase-6-complete` on origin
at `7afe309`). Phase 7 introduces the **typed ADK authoring
tools** — the LLM-facing surface that builds `ScheduleSpec`
drafts step-by-step via tool calls, per design §5.1 and §12
step 7 (post-renumber).

This phase is deliberately scoped to the **authoring surface
only**. It does NOT mount the toolset on `CoordinatorAgent`,
does NOT register an APScheduler binding, does NOT introduce
reasoning / emit dispatch (phase 12 post-renumber), does NOT
add source loaders (phase 10), and does NOT ship dry-run /
freeze (phase 8). The toolset is **test-rig only** until phase
9 cutover wires it to the production agent.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §5.1 (typed ADK tools), §5.5
  (validation chokepoint), §5.7 (registry cache), §12 step 7,
  §12.1 invariants 2 + 3.
- `docs/PHASE_6_PLAN.md` §1 out-of-scope items 1 + 5 (ADK tool
  surface + ChannelRef/SheetRef enum binding pointers).

---

## 0. Design-doc amendment

None expected. §5.1 + §5.5 already specify the tool list +
chokepoint contract. If reviewer rounds surface a gap, apply
the amendment as a plan-revision commit and update this §0
in that commit.

---

## 1. Scope statement

### In scope (phase 7)

1. **Draft storage** — `app/v2/authoring/drafts.py`.
   File-backed JSON at
   `tmp/v2_drafts/<session_id>/<draft_id>.json` with atomic
   `tempfile.mkstemp` + `os.rename` writes (mirrors the
   phase-6 loader). `DraftStore` class encapsulates the
   path resolver + CRUD helpers. Pydantic
   `ScheduleSpecDraft` model — a relaxed superset of
   `ScheduleSpec` whose required fields can be `None`
   while authoring is in flight; `to_spec()` converts to a
   `ScheduleSpec` and runs `validate_schedule_spec`.

2. **Setter tools** — `app/v2/authoring/setters.py`.
   Per-field mutation helpers each of which:
   - loads the draft via `DraftStore.read`,
   - applies the per-field update,
   - re-`to_spec` if the draft has enough fields to validate,
   - writes the draft back via `DraftStore.write`,
   - returns a `ToolResponse` dict (success / validation
     issues / not-yet-complete) for the LLM to read.

   Tools:
   - `schedule_draft_start(id, description, owner_platform,
     owner_user_id, owner_display_name=None) -> draft_id`
   - `schedule_set_description(draft_id, description)`
   - `schedule_set_owner(draft_id, platform, user_id, display_name=None)`
   - `schedule_set_cron(draft_id, cron_expr, timezone)`
   - `schedule_set_one_off(draft_id, at_iso_datetime, timezone)`
   - `schedule_set_failure_policy(draft_id, on_failure_action, retry_strategy=None)`

3. **Delivery setter + resolver wiring** —
   `app/v2/authoring/delivery.py`.
   `schedule_set_delivery(draft_id, channel_lookup,
   fallback_policy)` resolves `channel_lookup` against the
   phase-6 registry cache:
   - If the cache is absent (`load_cache` returns `None`)
     **and** a DI'd `SlackChannelsClient` is configured, the
     setter attempts a refresh.
   - If the cache is absent **and** the refresh raises (any
     `Exception`), the setter re-raises as
     `NoCacheAndNetworkDown(kind="slack_channels",
     network_error=<str(exc)>)` — closing the Q8 raise path
     phase 6 left to phase 7.
   - If the lookup hits via the resolver, the resolved
     `external_id` lands on the draft's `delivery.channel_ref`
     (raw-string `ChannelRef` per phase-1 model; the resolver
     guarantees the id exists at author time).
   - If the lookup misses on a present cache (`CacheMiss`),
     the setter returns the error in the `ToolResponse` so
     the LLM re-prompts the user.

4. **Compile + commit tools** —
   `app/v2/authoring/compile.py`.
   - `schedule_draft_compile(draft_id) -> ToolResponse`:
     funnels the draft through `validate_schedule_spec` and
     returns the result. Does NOT freeze (freeze gates on
     dry-run, phase 8).
   - `schedule_draft_commit(draft_id, conn) -> ToolResponse`:
     calls `validate_schedule_spec` then `insert_schedule`
     (phase-3 storage); on success deletes the draft file.
     This is the "ScheduleSpec lands in the v2 DB" verb.
     Until phase 9 cutover, no APScheduler job is registered;
     the spec sits in the v2 schedules table until a future
     phase mounts the binding and registers active rows.
     **Optional reviewer call:** push commit to phase 8 with
     freeze; default phase-7 includes it for test-rig parity.
   - `schedule_draft_discard(draft_id) -> ToolResponse`:
     deletes the draft file. Idempotent.
   - `schedule_draft_list(session_id) -> ToolResponse`:
     lists draft ids for a session.

5. **Lifecycle tools** — `app/v2/authoring/lifecycle.py`.
   Wrap the phase-3 storage helpers with a `ToolResponse`
   shape:
   - `schedule_pause(schedule_id, conn)`
   - `schedule_resume(schedule_id, conn)`
   - `schedule_archive(schedule_id, conn)`
   - `schedule_revive(schedule_id, conn)`

   Phase 7 does NOT call the phase-5
   `app.v2.runtime.lifecycle` hooks — those fire on the
   binding, which is not mounted in phase 7. Phase 9 cutover
   wires the hook calls.

6. **Toolset bundle** — `app/v2/toolsets/authoring.py`.
   ADK `BaseToolset` subclass exposing every authoring tool
   as `FunctionTool` instances. The toolset is NOT registered
   with `CoordinatorAgent`; tests instantiate it directly
   and invoke tools to drive the authoring flow.

7. **`ToolResponse` discriminated-union model** —
   `app/v2/authoring/responses.py`. Each tool returns one
   of:
   - `ToolResponse.ok(...)` — success payload.
   - `ToolResponse.validation_failed(issues)` — wraps the
     `ValidationResult.issues` list verbatim so the LLM can
     compose a fix.
   - `ToolResponse.not_ready(missing_fields)` — draft is not
     yet complete; documents which fields still need
     setting.
   - `ToolResponse.cache_unavailable(kind)` — the registry
     cache layer reported `NoCacheAndNetworkDown`. The LLM
     surfaces this verbatim to the user.
   - `ToolResponse.not_found(...)` — draft id or schedule id
     does not exist.

### Out of scope (phase 7)

- **`CoordinatorAgent` / `run_bot.py` mounting.** Phase 9
  cutover does this. Phase 7 ships the toolset class; nothing
  imports it from `app/agent.py` or `run_bot.py`.
- **`schedule_dry_run` + `schedule_freeze`.** Phase 8 (dry-run
  handshake per design §5.6).
- **ExecutionPlan authoring** (`plan_draft_*`,
  `plan_add_input_*`, `plan_add_reasoning`,
  `plan_add_emit_*`). Phases 10 (source loaders), 12
  (reasoning + emit dispatch).
- **`schedule_attach_execution_plan`.** Depends on plan
  authoring; lands when the plan-author phase ships.
- **Real Slack / Drive client construction.** Phase 7 uses
  the phase-6 `SlackChannelsClient` Protocol; tests pass a
  stub, dev rigs hand-wire a `slack_sdk` adapter. The
  production adapter ships at phase 9 cutover or later.
- **`ChannelRef` / `SheetRef` Pydantic-level enum binding.**
  The resolver is wired into authoring tools here; the
  field types stay as raw-string `external_id` /
  `spreadsheet_id`. The resolver-time validation IS the
  enum binding for phase 7.
- **Binding mount + scheduler integration.** Phase 9 cutover.
- **v1 paths / `run_bot.py` / v1 scheduler.** Design §12.1
  invariant 3 still in force.

---

## 2. New file paths

```
app/v2/authoring/__init__.py
app/v2/authoring/responses.py
app/v2/authoring/drafts.py
app/v2/authoring/setters.py
app/v2/authoring/delivery.py
app/v2/authoring/compile.py
app/v2/authoring/lifecycle.py

app/v2/toolsets/__init__.py        (new directory)
app/v2/toolsets/authoring.py

tests/v2/test_authoring_responses.py
tests/v2/test_authoring_drafts.py
tests/v2/test_authoring_setters.py
tests/v2/test_authoring_delivery.py
tests/v2/test_authoring_compile.py
tests/v2/test_authoring_lifecycle.py
tests/v2/test_authoring_toolset.py
```

Existing files touched:
- `.v2-current-phase` — bump 6 → 7.
- `scripts/check_phase_scope.py` — `PHASE_ALLOWLIST[7]`.
- `docs/PHASE_7_PLAN.md` — this file.

---

## 3. Module APIs

### 3.1 `responses.py`

```python
class ToolResponse(BaseModel):
    """Discriminated-union return shape for every authoring
    tool. The LLM reads ``status`` first and dispatches on
    payload keys.
    """

    status: Literal[
        "ok",
        "validation_failed",
        "not_ready",
        "cache_unavailable",
        "not_found",
    ]
    # Per-status payload keys; only the relevant one is
    # populated for each status.
    draft_id: Optional[str] = None
    schedule_id: Optional[str] = None
    spec: Optional[dict] = None  # ScheduleSpec.model_dump
    issues: Optional[list[ValidationIssue]] = None
    missing_fields: Optional[list[str]] = None
    cache_kind: Optional[str] = None
    network_error: Optional[str] = None
    message: Optional[str] = None  # human-readable hint

    @classmethod
    def ok(cls, **kw) -> "ToolResponse":
        return cls(status="ok", **kw)

    @classmethod
    def validation_failed(cls, issues) -> "ToolResponse":
        return cls(status="validation_failed", issues=issues)

    @classmethod
    def not_ready(cls, missing_fields) -> "ToolResponse":
        return cls(status="not_ready", missing_fields=missing_fields)

    @classmethod
    def cache_unavailable(cls, kind, network_error) -> "ToolResponse":
        return cls(
            status="cache_unavailable",
            cache_kind=kind,
            network_error=network_error,
        )

    @classmethod
    def not_found(cls, *, message) -> "ToolResponse":
        return cls(status="not_found", message=message)
```

### 3.2 `drafts.py`

```python
class ScheduleSpecDraft(BaseModel):
    """Relaxed superset of ScheduleSpec — every required
    field is Optional while authoring is in flight."""

    model_config = ConfigDict(extra="forbid")
    id: str
    description: Optional[str] = None
    owner: Optional[UserRef] = None
    trigger: Optional[Trigger] = None
    delivery: Optional[Delivery] = None
    failure: Optional[FailurePolicy] = None
    audit: AuditPolicy = AuditPolicy()
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    execution_plan_hash: Optional[str] = None
    authored_at: Optional[str] = None  # filled at compile()

    def missing_required_fields(self) -> list[str]: ...
    def to_spec(self, *, clock: Callable[[], datetime]) -> ScheduleSpec:
        """Build a fresh ScheduleSpec, set authored_at via
        clock(), call with_fresh_hash(). Raises ValueError if
        a required field is None."""


class DraftStore:
    """File-backed draft persistence under
    ``tmp/v2_drafts/<session_id>/<draft_id>.json``. Atomic
    writes via tempfile.mkstemp + os.rename (same pattern
    as phase-6 loader)."""

    def __init__(
        self,
        *,
        base: Optional[pathlib.Path] = None,
    ) -> None: ...

    def read(self, session_id: str, draft_id: str) -> ScheduleSpecDraft: ...
    def write(self, session_id: str, draft: ScheduleSpecDraft) -> None: ...
    def delete(self, session_id: str, draft_id: str) -> None: ...
    def list_ids(self, session_id: str) -> list[str]: ...
```

The base path is `tmp/v2_drafts/` to match the v1 convention
(`tmp/plans/`, `tmp/scratchpads/`) so the existing
`tmp_sweeper` (if any) still cleans drafts. Tests override
via `base=tmp_path`.

### 3.3 `setters.py`

Each setter is an async function with the documented
signature; each one:
1. Loads via `DraftStore.read`.
2. Mutates the draft.
3. Validates the partial draft if `missing_required_fields`
   is empty (else returns `ToolResponse.not_ready`).
4. Writes via `DraftStore.write`.
5. Returns `ToolResponse.ok(draft_id=...)`.

`schedule_draft_start` is the only setter that creates a
fresh `ScheduleSpecDraft`.

### 3.4 `delivery.py`

```python
async def schedule_set_delivery(
    draft_id: str,
    *,
    channel_lookup: str,
    fallback_policy: DeliveryFallbackPolicy,
    session_id: str,
    store: DraftStore,
    cache_loader: Callable[[], Optional[SlackChannelsCache]],
    slack_client: Optional[SlackChannelsClient],
    clock: Callable[[], datetime],
    expected_owner_id: str,
) -> ToolResponse:
    """Resolve ``channel_lookup`` against the registry cache.

    Flow:
    1. ``cache = cache_loader()``.
    2. If ``cache is None``:
       a. If ``slack_client is None`` → raise
          ``NoCacheAvailable`` (path=None); the tool catches
          and returns ``ToolResponse.cache_unavailable``.
       b. Else: ``refresh_slack_channels(slack_client,
          expected_owner_id=..., clock=clock)``. On
          ``Exception`` → wrap as
          ``NoCacheAndNetworkDown(kind="slack_channels",
          network_error=str(exc))`` and return
          ``ToolResponse.cache_unavailable``.
       c. Save the fresh cache via ``save_cache``.
    3. Call ``resolve_channel(channel_lookup, cache)``:
       - ``CacheMiss`` → return ``ToolResponse.validation_failed``
         with a synthetic ValidationIssue naming the lookup.
       - ``ChannelAmbiguous`` → return
         ``ToolResponse.validation_failed`` listing the
         candidate ids in the issue payload.
    4. On success, build the ChannelRef with
       ``external_id = entry.id``, save the draft,
       return ``ToolResponse.ok(draft_id=...)``.
    """
```

The DI shape (`cache_loader`, `slack_client`,
`expected_owner_id`) makes the function testable without
real Slack credentials. The phase-9 cutover supplies
production wiring; phase 7 provides a thin factory that
defaults to phase-6's `load_cache` + an env-derived
`expected_owner_id`.

### 3.5 `compile.py`

```python
async def schedule_draft_compile(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Validate the draft and return the canonical
    ScheduleSpec body (does NOT freeze).

    Workflow:
    1. ``draft = store.read(session_id, draft_id)``.
    2. ``missing = draft.missing_required_fields()``. If
       non-empty → ``ToolResponse.not_ready(missing_fields=missing)``.
    3. ``spec = draft.to_spec(clock=clock)``.
    4. ``result = validate_schedule_spec(spec, registry=...)``.
       (Registry param comes from the existing phase-3
       ``RegistrySnapshot`` shape — DI'd.)
    5. If ``result.issues``: ``ToolResponse.validation_failed``.
    6. Else: ``ToolResponse.ok(spec=spec.model_dump())``.

    Optional follow-up (slice 5 reviewer call): wire
    ``schedule_draft_commit`` to call this then
    ``insert_schedule(conn, spec)``. Default: ship the
    commit verb here so the test rig can land a row; the
    binding mount remains a phase-9 concern.
    """


async def schedule_draft_commit(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
    conn: sqlite3.Connection,
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Compile + insert. On success deletes the draft."""


async def schedule_draft_discard(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
) -> ToolResponse: ...


async def schedule_draft_list(
    session_id: str,
    *,
    store: DraftStore,
) -> ToolResponse: ...
```

### 3.6 `lifecycle.py`

```python
async def schedule_pause(
    schedule_id: str, *, conn: sqlite3.Connection
) -> ToolResponse:
    """Calls
    ``app.v2.storage.schedules.update_schedule_status(conn,
    schedule_id, ScheduleStatus.PAUSED)``. Returns
    ``ToolResponse.ok(schedule_id=...)`` on success; raises
    ``ToolResponse.not_found`` shape on missing row.
    """


async def schedule_resume(...): ...   # ScheduleStatus.ACTIVE
async def schedule_archive(...): ...  # ScheduleStatus.ARCHIVED
async def schedule_revive(...): ...   # archived → active path
```

Phase 7 does NOT call `app.v2.runtime.lifecycle` hooks; the
binding is not mounted. Phase 9 cutover wires the hook
calls alongside the storage update.

### 3.7 `app/v2/toolsets/authoring.py`

```python
class AuthoringToolset(BaseToolset):
    """ADK toolset bundling every authoring + lifecycle
    tool as a FunctionTool. Holds the DI'd ``DraftStore``,
    ``cache_loader``, ``slack_client``, ``clock``, and
    ``expected_owner_id`` as constructor args so tests can
    instantiate the toolset against a tmp_path draft store +
    stub Slack client.

    NOT mounted on any agent in phase 7.
    """

    def __init__(
        self,
        *,
        store: DraftStore,
        clock: Callable[[], datetime],
        slack_client: Optional[SlackChannelsClient] = None,
        expected_owner_id: Optional[str] = None,
        registry: RegistrySnapshot,
    ) -> None: ...

    async def get_tools(self, ctx=None) -> list[FunctionTool]: ...
```

The toolset uses ADK's `BaseToolset` per the existing
`app/toolsets/*` convention; tools register via
`FunctionTool(func=...)`.

---

## 4. Slice ordering + commit cadence

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan) | `docs/PHASE_7_PLAN.md` + `.v2-current-phase` 6 → 7 + `PHASE_ALLOWLIST[7]` | (none) |
| 1 | `responses.py` + `drafts.py` (ScheduleSpecDraft + DraftStore) | `test_authoring_responses.py` + `test_authoring_drafts.py` |
| 2 | `setters.py` (start / set_description / set_owner / set_cron / set_one_off / set_failure_policy) | `test_authoring_setters.py` |
| 3 | `delivery.py` (set_delivery + resolver wiring + NoCacheAndNetworkDown raise path) | `test_authoring_delivery.py` |
| 4 | `compile.py` (compile / commit / discard / list) | `test_authoring_compile.py` |
| 5 | `lifecycle.py` (pause / resume / archive / revive) | `test_authoring_lifecycle.py` |
| 6 | `app/v2/toolsets/authoring.py` + package smoke | `test_authoring_toolset.py` |
| closeout | acceptance + tag `v2-phase-7-complete` (gated on codex pass) | — |

Slice 6 introduces the new `app/v2/toolsets/` directory. If
phase-2's `app/v2/registry.py` `ToolRegistry` is the
canonical surface for tool descriptors, slice 6 also
registers the new tools' `ToolDescriptor` entries (with
appropriate metadata tags per design §5.4) so phase-9
cutover finds them via the registry.

---

## 5. Test inventory

Detailed tests live per-slice. Test counts are estimates;
plan revision rounds may add pins.

### 5.1 `test_authoring_responses.py`

- Each factory (`ok` / `validation_failed` / `not_ready` /
  `cache_unavailable` / `not_found`) returns a
  `ToolResponse` with the documented `status` discriminator.
- Only the relevant per-status payload key is populated;
  others are `None`.
- Round-trip via `model_dump_json` / `model_validate_json`
  preserves every populated field.
- Unknown `status` value → `ValidationError`.

### 5.2 `test_authoring_drafts.py`

- `DraftStore.write` then `read` round-trips a draft.
- `DraftStore.read` on missing file → `FileNotFoundError`
  (caller wraps as `ToolResponse.not_found`).
- `DraftStore.delete` is idempotent.
- `list_ids` returns the draft ids in deterministic order.
- Atomic write: simulated crash mid-rename preserves the
  previous draft (mirror phase-6 pin).
- Concurrent same-pid writes do not collide (mirror phase-6
  pin via `tempfile.mkstemp`).
- `ScheduleSpecDraft.missing_required_fields` reports the
  unset required fields list.
- `to_spec` raises when required fields are unset.
- `to_spec` calls `clock()` to populate `authored_at` (AST
  pin — no `datetime.now` in the function body, mirrors the
  phase-6 `is_stale` pin).
- Round-trip through `to_spec` + `validate_schedule_spec`
  for a complete draft returns no issues.

### 5.3 `test_authoring_setters.py`

- `schedule_draft_start` creates a draft file with the
  documented fields populated.
- Each setter loads / mutates / writes; pin by reading
  back the file after the call.
- Setter on missing draft → `ToolResponse.not_found`.
- Setter on invalid input (e.g. naive datetime to
  `set_one_off`) → `ToolResponse.validation_failed` with the
  underlying ValidationIssue.
- `schedule_set_cron` with a numeric DOW string →
  `ToolResponse.validation_failed` (the phase-5 `cron_guard`
  catches it).
- Partially populated draft → `ToolResponse.not_ready` with
  the `missing_fields` list.

### 5.4 `test_authoring_delivery.py`

- Cache present, channel lookup hits → `ToolResponse.ok`;
  draft's `delivery.channel_ref.external_id` matches the
  resolved id.
- Cache present, `CacheMiss` → `ToolResponse.validation_failed`
  with a ValidationIssue naming the lookup.
- Cache present, `ChannelAmbiguous` →
  `ToolResponse.validation_failed` with the candidate_ids in
  the issue payload.
- Cache absent + `slack_client=None` →
  `ToolResponse.cache_unavailable` with
  `cache_kind="slack_channels"` and a descriptive
  `network_error`.
- Cache absent + `slack_client` available + refresh
  succeeds → fresh cache saved; resolution proceeds; tool
  returns `ToolResponse.ok`.
- Cache absent + `slack_client.list_conversations` raises →
  `ToolResponse.cache_unavailable` with
  `network_error=str(exc)`.
- AST + sys.modules pins: the delivery module does NOT
  import `slack_sdk` at module load (Protocol-typed DI).

### 5.5 `test_authoring_compile.py`

- Complete draft compiles to a valid `ScheduleSpec`; tool
  returns `ToolResponse.ok` with `spec` dict.
- Incomplete draft → `ToolResponse.not_ready`.
- Validation failure (e.g. hash drift) →
  `ToolResponse.validation_failed`.
- `schedule_draft_commit` inserts the row via
  `insert_schedule` and deletes the draft file.
- `schedule_draft_commit` on validation failure leaves the
  draft file in place; no row inserted.
- `schedule_draft_discard` deletes the draft file;
  idempotent (second call returns `ToolResponse.ok` with a
  "already absent" hint).
- `schedule_draft_list` returns the current draft ids;
  empty list when no drafts.

### 5.6 `test_authoring_lifecycle.py`

- Each lifecycle tool updates the schedule status via the
  phase-3 storage helper; pin by querying the row after.
- Pause on missing schedule → `ToolResponse.not_found`.
- Pause on already-paused schedule → idempotent
  `ToolResponse.ok` (no error).
- Phase-5 lifecycle hooks are NOT called by phase-7 tools
  — pin via patching the hook module and asserting zero
  calls (the binding-mount integration is phase-9 work).

### 5.7 `test_authoring_toolset.py`

- `AuthoringToolset.get_tools` returns a non-empty list of
  `FunctionTool` instances covering every documented tool
  name.
- Each tool's `name` / `description` are non-empty (ADK
  registry hygiene).
- Toolset does NOT auto-register with any agent on import.
- Smoke: import every public symbol from
  `app.v2.authoring` and `app.v2.toolsets.authoring`;
  no module imports `slack_sdk` / `googleapiclient` at
  module load.

---

## 6. CI guard checks

`scripts/check_phase_scope.py` gains:

```python
7: {
    "app/v2/",
    "tests/v2/",
    "scripts/check_phase_scope.py",
    "scripts/install_hooks.py",
    ".githooks/v2_phase_guard.sh",
    ".githooks/pre-commit",
    ".github/workflows/v2_phase_guard.yml",
    ".v2-current-phase",
    "docs/PHASE_7_PLAN.md",
    "docs/CONTRACTS_V2_DESIGN.md",
    ".docs_read_marker",
},
```

The v1-paths-forbidden rule (§12.1 invariant 3) carries
forward — phase 7 cannot touch `app/contracts/`,
`app/tasks.py`, `app/contracts/executor.py`,
`app/scheduler_instance.py`, `data/contracts/`.

Cross-cutting smoke checks added or carried:

- Phase-7 modules import no I/O libs at module load
  (`slack_sdk` / `googleapiclient` / `httpx` / `requests`
  / `urllib3` / `aiohttp` / `smtplib` / `subprocess`).
  Phase-6 carries forward; phase 7's delivery module uses
  the phase-6 Protocol-typed DI.
- Phase-7 modules do NOT import
  `app.v2.runtime._defaults` at module load. Clock is a
  DI'd parameter throughout.
- No phase-7 module dispatches to reasoning / emit /
  sub-agents / delegate / transfer / fire / claim / execute
  (carried from phase 4-6). The compile + commit tools
  call `insert_schedule` (storage write) only.
- Toolset module does NOT auto-mount on any agent (no
  side-effect imports of `app/agent.py` / `run_bot.py`).

---

## 7. Acceptance criteria for `v2-phase-7-complete`

1. Branch ahead of `v2-phase-6-complete` by N small commits,
   each scoped to one of the slices in §4.
2. `ToolResponse` ships with the five documented statuses;
   round-trip + factory pins green.
3. `ScheduleSpecDraft` + `DraftStore` ship with atomic
   mkstemp+rename writes; same-pid concurrent write
   regression test green; AST pin on `to_spec` (no
   `datetime.now`).
4. Six setters (`schedule_draft_start` / `set_description` /
   `set_owner` / `set_cron` / `set_one_off` /
   `set_failure_policy`) call `validate_schedule_spec`
   after mutation when the draft is complete; return
   `ToolResponse.not_ready` while still in flight.
5. `schedule_set_delivery` resolves the channel via the
   phase-6 cache; cache-absent + refresh-failure raises
   `NoCacheAndNetworkDown` internally and returns
   `ToolResponse.cache_unavailable` (Q8 raise path closed).
6. `schedule_draft_compile` / `schedule_draft_commit` /
   `schedule_draft_discard` / `schedule_draft_list` ship;
   commit funnels through `validate_schedule_spec` and
   `insert_schedule`.
7. Four lifecycle tools update schedule status via the
   phase-3 storage helper; idempotent; no phase-5 binding
   hooks called.
8. `AuthoringToolset` exposes every tool as a
   `FunctionTool` via `BaseToolset.get_tools`. NOT
   registered with any agent.
9. Phase guard `--diff v2-phase-6-complete` clean.
10. No v1 paths touched. `run_bot.py` untouched.
    `boot_runtime` untouched. `CoordinatorAgent` untouched.
11. No `datetime.now()` / `uuid.uuid4()` outside
    `_defaults.py`. AST pin on every authoring module.
12. Full v2 test suite passes (existing 1366 + phase-7
    adds); no regressions.
13. Annotated git tag `v2-phase-7-complete` created and
    pushed (workflow pre-approved per phase 6 kickoff).

---

## 8. Tag annotation

```
v2 phase 7 complete

Typed ADK authoring tools that build ScheduleSpec drafts
step-by-step. File-backed draft storage with atomic writes;
field setters that call validate_schedule_spec after every
mutation; channel-resolution path that funnels through the
phase-6 registry cache and raises NoCacheAndNetworkDown
when both cache and refresh are unavailable; compile +
commit verbs that land a row in the v2 schedules table;
lifecycle tools (pause / resume / archive / revive) over
phase-3 storage helpers. Toolset bundle in
app/v2/toolsets/authoring.py exposes every tool as a
FunctionTool but is NOT mounted on any agent.

NO production agent mounting. CoordinatorAgent untouched;
boot_runtime untouched; no binding wiring. ExecutionPlan
authoring (reasoning + source + emit) deferred to phases
10 / 12. Dry-run + freeze deferred to phase 8. v1 still owns
production wakeup until phase 9 cutover.

Design: docs/CONTRACTS_V2_DESIGN.md §5.1, §5.5, §5.7,
        §12 step 7
Plan:   docs/PHASE_7_PLAN.md
```

---

## 9. Open questions

### 9.1 Closed in this revision

1. ~~Draft storage shape.~~ **CLOSED.** File-backed JSON at
   `tmp/v2_drafts/<session_id>/<draft_id>.json`, atomic
   `mkstemp`+`rename` writes (mirrors phase-6 loader).
   SQLite + new migration ruled out for now — drafts are
   ephemeral; the existing tmp/ convention matches v1's
   planner.
2. ~~`ChannelRef` / `SheetRef` enum binding.~~ **CLOSED.**
   The resolver call at authoring time IS the enum binding
   for phase 7. The field types stay weakly typed
   (`external_id: str`); the authoring layer guarantees the
   id exists via `resolve_channel`. A future phase can
   tighten to a Pydantic enum if reviewer wants.
3. ~~`NoCacheAndNetworkDown` raise path.~~ **CLOSED.**
   `schedule_set_delivery` catches the
   `NoCacheAvailable` + refresh-failure pair and raises
   internally; the tool returns
   `ToolResponse.cache_unavailable` to the LLM.
4. ~~Toolset agent mounting.~~ **CLOSED.** Phase 7 ships
   the toolset class; mounting on `CoordinatorAgent` is
   phase 9 cutover work.

### 9.2 Still open

5. **`schedule_draft_commit` scope.** Default phase-7 ships
   the commit verb so the test rig can land a v2 schedule
   row without depending on phase-8's freeze. Reviewer call:
   keep here or push to phase 8 alongside freeze?

6. **Validation chokepoint signature.** The existing
   `validate_schedule_spec` takes `(spec, registry)`. Phase
   7 needs a `RegistrySnapshot`. The simplest path: an
   empty / minimal registry that knows about no plan
   descriptors (phase 7 doesn't author ExecutionPlans).
   Reviewer call: ship a phase-7 helper that builds an
   empty `RegistrySnapshot`, or expect the test rig to
   construct one?

7. **`AuthoringToolset` registry integration.** Phase-2's
   `ToolRegistry` lives at `app/v2/registry.py`. Should
   phase 7 add `ToolDescriptor` entries for every
   authoring tool, or wait for phase 9 to do that when
   binding mounts? Default: register descriptors here so
   phase 9's wiring sees them.

8. **Setter return-shape on a partially populated draft.**
   Plan default: validation runs ONLY when the draft is
   complete; otherwise return `not_ready`. Reviewer call:
   should setters always run partial-validation and return
   any catchable issues immediately?

9. **`schedule_set_delivery` cache-save side effect.**
   When the cache is refreshed inside the setter, plan
   default is to save it to disk via `save_cache` so
   subsequent setters reuse the fresh state. Reviewer call:
   keep the side effect or surface a separate
   `schedule_refresh_channel_cache` tool the LLM calls
   explicitly?

10. **`expected_owner_id` source.** Production needs a
    workspace id (Slack T-id). Phase-7 plan defaults to a
    DI'd `expected_owner_id` constructor arg on the
    toolset. Reviewer call: env-derived default in phase
    7, or defer to phase 9 cutover?

---

## 10. Hard rules (carried forward)

Same as phase 6 plan §10. Restated for self-containment:

1. No push without explicit reviewer / Sergey approval.
2. No edits to phase-1 through phase-6 plan docs without a
   `PHASE_OVERRIDE:` mechanism in the commit message.
3. No time estimates.
4. Pause after each commit for reviewer.
5. `uv run python …` always.
6. Pre-commit hook needs `.docs_read_marker` —
   `echo "yes" | uv run python scripts/check_docs_read.py`.
7. Commit messages end with
   `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
8. Leave `app/tools/youtube.py` dirty/uncommitted unless
   reviewer flags otherwise; same for the three diag
   scripts at the repo root (`scripts/amazon_ads_mcp_proxy.py`,
   `scripts/diag_gemini_caching.py`, `scripts/diag_removal_order.py`).
9. No v1 scheduler edits (carried until phase 9 cutover
   post-renumber).
10. Every runtime / authoring helper takes injected clock
    + id factories; `_defaults.py` is the ONLY module that
    wires them to wall clock + uuid4. Phase-7 setters and
    `to_spec` take `clock` as a parameter.
