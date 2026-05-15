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
   `tmp/v2_drafts/<session_id_slug>/<draft_id_slug>.json`
   with atomic `tempfile.mkstemp` + `os.rename` writes
   (mirrors the phase-6 loader). `DraftStore` class
   encapsulates the path resolver + CRUD helpers.

   **Path-traversal fence (round-2 reviewer L41 fix):**
   `session_id` and `draft_id` are user / LLM-supplied
   strings; without a fence a value like `"../../etc"`
   would escape the base directory. Two coordinated checks:
   - **Slug regex:** every id segment must match
     `^[A-Za-z0-9_-]{1,128}$`. Anything else raises
     `ValueError("invalid session_id"|"invalid draft_id")`
     BEFORE any I/O.
   - **`Path.resolve()` guard:** after building the target
     path, `resolved.is_relative_to(base.resolve())` MUST be
     True. A symlink that points outside the base is
     refused by this check.

   Pydantic `ScheduleSpecDraft` model — a relaxed superset of
   `ScheduleSpec` whose required fields can be `None`
   while authoring is in flight; `to_spec(*, clock)` converts
   to a `ScheduleSpec` (calling `with_fresh_hash()`).
   `to_spec` does NOT call `validate_schedule_spec` itself —
   that's the compile-tool's job (round-2 reviewer Q6
   answer: the chokepoint takes
   `validate_schedule_spec(spec)` directly with no helper
   wrapper for the reminder-only flow phase 7 ships).

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

4. **Compile + list + discard tools** —
   `app/v2/authoring/compile.py`. **Commit verb is OUT of
   phase 7 (round-2 reviewer L95 fix, Q5 answer):** landing
   a row in the schedules table before phase-8's dry-run +
   freeze handshake would contradict the design freeze
   gate AND would bypass the `schedule_created` event
   ledger entry the EventLedger needs. Phase 7 ships only
   the verbs that do NOT mutate the v2 DB; phase 8 owns
   the `freeze + commit + schedule_created event` atomic
   triplet.

   - `schedule_draft_compile(draft_id) -> ToolResponse`:
     funnels the draft through `validate_schedule_spec`
     (with `execution_plans=None, registries=None` —
     reminder-only flow per Q6) and returns the result.
     Does NOT write to the v2 DB and does NOT freeze.
     Returns `ToolResponse.ok(spec=spec.model_dump())` on
     success so the LLM (and downstream phase-8 freeze)
     sees the canonical body.
   - `schedule_draft_discard(draft_id) -> ToolResponse`:
     deletes the draft file. Idempotent.
   - `schedule_draft_list(session_id) -> ToolResponse`:
     lists draft ids for a session.

5. **Lifecycle tools** — `app/v2/authoring/lifecycle.py`.
   **Round-2 reviewer L109 + L442 + L598 fix:** lifecycle
   tools must (a) emit the matching EventLedger event in
   the SAME transaction as the status update, (b) honour
   the documented archived→PAUSED revive transition (admin
   re-approves before resume), and (c) cancel pending Runs
   when a schedule is archived.

   New helper in `app/v2/storage/schedules.py` (or a thin
   wrapper module under `app/v2/authoring/`):

   ```python
   def update_status_with_event(
       conn: sqlite3.Connection,
       schedule_id: str,
       new_status: ScheduleStatus,
       event_kind: EventKind,
       *,
       event_id_factory: Callable[[], str],
       clock: Callable[[], datetime],
       cancel_pending_runs: bool = False,
   ) -> None:
       """Atomic status flip + EventLedger append. When
       ``cancel_pending_runs=True`` also flips every pending
       Run for this schedule to ``cancelled`` and appends a
       ``run_cancelled`` event for each — all in one
       transaction. Used by ``schedule_archive``.
       """
   ```

   The four authoring tools:
   - `schedule_pause(schedule_id, conn, *, event_id_factory, clock)`
     — active → paused. Emits ``schedule_paused``.
   - `schedule_resume(schedule_id, conn, *, event_id_factory, clock)`
     — paused → active. Emits ``schedule_resumed``. Refuses
     when current status is archived (design §11.4: admin
     must revive to PAUSED first).
   - `schedule_archive(schedule_id, conn, *, event_id_factory, clock)`
     — active / paused → archived. Emits
     ``schedule_archived``. Cancels every pending Run for
     this schedule (pending → cancelled + ``run_cancelled``
     event per Run) in the same transaction. Implements the
     design-required atomic cancel.
   - `schedule_revive(schedule_id, conn, *, event_id_factory, clock)`
     — archived → **PAUSED** (round-2 reviewer L442 fix —
     not directly back to active). Emits
     ``schedule_revived``. Caller must explicitly call
     ``schedule_resume`` afterwards to flip to active; the
     two-step ensures the admin re-approves before fires
     resume.

   Phase 7 does NOT call the phase-5
   `app.v2.runtime.lifecycle` hooks — those fire on the
   binding, which is not mounted in phase 7. Phase 9 cutover
   wires the hook calls alongside the status-and-event
   helper.

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
tests/v2/test_authoring_lifecycle_helper.py
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
signature; each one (round-2 reviewer Q8 answer — validate
the changed field immediately; full
``validate_schedule_spec`` only when the draft is complete):

1. Loads via `DraftStore.read`.
2. **Per-field validation BEFORE mutation:** construct the
   field-typed Pydantic model (e.g. `CronTrigger(...)`,
   `UserRef(...)`, `FailurePolicy(...)`) from the raw
   arguments. A `ValidationError` here →
   `ToolResponse.validation_failed` with the pydantic
   issues mapped to `ValidationIssue` shape. This catches
   typos (e.g. naive ISO datetime to `set_one_off`, numeric
   DOW to `set_cron` via the phase-5 `cron_guard`) the
   moment the LLM passes a bad arg.
3. Apply the field update to the draft.
4. If `draft.missing_required_fields()` is empty AND the
   final-spec rules apply: build the spec via `to_spec` and
   call `validate_schedule_spec(spec)`. Issues →
   `ToolResponse.validation_failed`; else proceed.
5. If the draft is still incomplete: skip the full-spec
   validate and write the partial draft.
6. Writes via `DraftStore.write`.
7. Returns `ToolResponse.ok(draft_id=...)` or
   `ToolResponse.not_ready(missing_fields=...)` when the
   write succeeded but the draft is not yet a complete spec.

`schedule_draft_start` is the only setter that creates a
fresh `ScheduleSpecDraft` (and writes the seed file).

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
    cache_saver: Callable[[SlackChannelsCache], None],
    slack_client: Optional[SlackChannelsClient],
    clock: Callable[[], datetime],
    expected_owner_id: str,
) -> ToolResponse:
    """Resolve ``channel_lookup`` against the registry cache.

    All DI args are REQUIRED (round-2 reviewer L365 / Q10):
    no env-derived defaults sneak into phase 7. The toolset
    (§3.7) constructs the loader / saver / client closures
    against its constructor args.

    Flow:
    1. ``cache = cache_loader()``.
    2. If ``cache is None``:
       a. If ``slack_client is None`` → return
          ``ToolResponse.cache_unavailable(kind="slack_channels",
          network_error="no client configured")``.
       b. Else: ``refresh_slack_channels(slack_client,
          expected_owner_id=expected_owner_id, clock=clock)``.
          On ``Exception`` → return
          ``ToolResponse.cache_unavailable(kind="slack_channels",
          network_error=str(exc))`` (raise path closes the
          phase-6 Q8 NoCacheAndNetworkDown contract at this
          layer).
       c. Save the fresh cache via ``cache_saver(cache)``
          (Q9 — explicit saver DI; the auto-save side
          effect stays but the path/saver are caller-supplied
          so tests can pin or stub).
    3. Call ``resolve_channel(channel_lookup, cache)``:
       - ``CacheMiss`` → return ``ToolResponse.validation_failed``
         with a synthetic ValidationIssue naming the lookup.
       - ``ChannelAmbiguous`` → return
         ``ToolResponse.validation_failed`` listing the
         candidate ids in the issue payload.
    4. On success, build the ChannelRef with
       ``external_id = entry.id`` + ``kind = "slack"``, save
       the draft, return ``ToolResponse.ok(draft_id=...)``.
    """
```

The DI shape (`cache_loader`, `cache_saver`, `slack_client`,
`expected_owner_id`, `clock`) makes the function testable
without real Slack credentials AND without disk I/O if the
test stubs both loader and saver. Phase 9 cutover supplies
production wiring; phase 7 does NOT auto-derive any of these
from env or globals.

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
    ScheduleSpec body (does NOT freeze, does NOT write to
    the v2 DB — round-2 reviewer L95).

    Workflow:
    1. ``draft = store.read(session_id, draft_id)``.
    2. ``missing = draft.missing_required_fields()``. If
       non-empty → ``ToolResponse.not_ready(missing_fields=missing)``.
    3. ``spec = draft.to_spec(clock=clock)``.
    4. ``result = validate_schedule_spec(spec)`` — no
       ``execution_plans`` and no ``registries`` (round-2
       reviewer L386 + Q6: the reminder-only flow phase 7
       ships does not author ExecutionPlans, so both
       optional args stay ``None`` and the validator skips
       the plan / adapter rules). Future plan-backed flows
       DI'd ``execution_plans`` + ``registries`` then.
    5. If ``result.ok`` is False:
       ``ToolResponse.validation_failed(issues=result.issues)``.
    6. Else: ``ToolResponse.ok(spec=spec.model_dump())``.

    **Commit verb deferred to phase 8** alongside the
    freeze + ``schedule_created`` event triplet (round-2
    reviewer L95 + Q5). Phase 7 ships compile + discard +
    list only.
    """


async def schedule_draft_discard(
    draft_id: str,
    *,
    session_id: str,
    store: DraftStore,
) -> ToolResponse:
    """Delete the draft file. Idempotent — repeat call
    returns ``ToolResponse.ok`` with a "already absent"
    hint in ``message``."""


async def schedule_draft_list(
    session_id: str,
    *,
    store: DraftStore,
) -> ToolResponse:
    """Return the draft ids for ``session_id`` in
    deterministic order (sorted lexicographically). Empty
    session → ``ToolResponse.ok`` with an empty list."""
```

### 3.6 `lifecycle.py`

Round-2 reviewer L109 + L442 + L598 fix: every lifecycle
tool funnels through a single atomic helper that flips the
status AND appends the matching EventLedger event in one
transaction. ``schedule_archive`` additionally cancels every
pending Run in the same transaction.

```python
async def schedule_pause(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Active → paused. Atomic status flip + ``schedule_paused``
    event append.

    Returns ``ToolResponse.ok(schedule_id=...)`` on success;
    ``ToolResponse.not_found`` on missing row.

    Idempotent against already-paused: the second call still
    appends a ``schedule_paused`` event (audit trail logs the
    operator action even if the status was unchanged).
    Reviewer call open in §9.2 #15.
    """


async def schedule_resume(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Paused → active. Atomic status flip +
    ``schedule_resumed`` event append.

    Refuses (returns ``ToolResponse.validation_failed`` with
    a synthetic ValidationIssue) when the current status is
    ``archived`` — design §11.4 / round-2 reviewer L442
    requires admin to ``schedule_revive`` to PAUSED first
    so a re-approval gate exists.
    """


async def schedule_archive(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Active / paused → archived. Atomic in one TX:

    1. Flip schedule status to ``archived`` + append
       ``schedule_archived`` event.
    2. For every pending Run with ``schedule_id = X``: flip
       Run status to ``cancelled`` + append a
       ``run_cancelled`` event with reason
       ``"schedule_archived"``. Each Run gets its own event
       id from ``event_id_factory()``.

    The atomic cancellation is the design-required
    "archived schedules cancel existing pending Runs"
    behaviour (round-2 reviewer L598). Returns
    ``ToolResponse.ok`` with a payload field describing
    how many pending Runs were cancelled.
    """


async def schedule_revive(
    schedule_id: str,
    *,
    conn: sqlite3.Connection,
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Archived → PAUSED (round-2 reviewer L442). The
    two-step archived → paused → active gate ensures an
    admin re-approves before any new fires happen.

    Atomic status flip + ``schedule_revived`` event append.
    Caller must explicitly call ``schedule_resume`` to
    move the spec to ``active``.
    """
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
        event_id_factory: Callable[[], str],
        expected_owner_id: str,           # REQUIRED — no env default (round-2 L365 / Q10).
        slack_client: Optional[SlackChannelsClient] = None,
        cache_base: Optional[pathlib.Path] = None,  # registry-cache base path; DI'd (Q9).
    ) -> None:
        """No ``registries`` kwarg — phase 7 calls
        ``validate_schedule_spec(spec)`` with neither
        ``execution_plans`` nor ``registries`` (Q6 reminder-
        only flow). Future plan-backed flows extend this
        signature."""

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
| 4 | `compile.py` (compile / discard / list — NO commit, per round-2 L95 / Q5) | `test_authoring_compile.py` |
| 5a | `update_status_with_event` helper in storage + tests | new helper tests in `test_authoring_lifecycle_helper.py` |
| 5b | `lifecycle.py` (pause / resume / archive / revive) with event-id factory DI, archive cancellation of pending Runs, archived → PAUSED revive | `test_authoring_lifecycle.py` |
| 6 | `app/v2/toolsets/authoring.py` + ToolDescriptor registration (Q7) + package smoke | `test_authoring_toolset.py` |
| closeout | acceptance + tag `v2-phase-7-complete` (gated on codex pass) | — |

Slice 5 is split into the **5a helper** (atomic
`update_status_with_event` over the schedules + events
tables, with the pending-Run cancellation branch for the
archive path) and the **5b lifecycle tools** that consume
it. The split is so the helper's transaction semantics
(rollback on any single sub-step failure) get focused
reviewer attention before four tools depend on it.

Slice 6 introduces the new `app/v2/toolsets/` directory.
Per Q7 answer it ALSO registers every authoring tool as a
`ToolDescriptor` in `app/v2/registry.py:ToolRegistry` with
the appropriate metadata tags per design §5.4 (mostly
`write_external` for the setters/lifecycle/compile, plus
`uses_oauth` on `schedule_set_delivery` because it can
trigger a Slack API call). Metadata-only registration —
no agent mount in phase 7.

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
- **Path-traversal fence (L41 fix):**
  - `DraftStore.read(session_id="../etc", draft_id="x")`
    → `ValueError("invalid session_id")`.
  - `DraftStore.write(session_id="ok", draft=Draft(id="../a"))`
    → `ValueError("invalid draft_id")`.
  - Absolute-path id (`"/tmp/foo"`) rejected.
  - Backslash traversal (`"..\\etc"`) rejected.
  - Empty string rejected.
  - String of 129 chars (slug regex max 128) rejected.
  - Symlink that points outside the base directory →
    `ValueError` from the `is_relative_to(base.resolve())`
    guard. Test creates `tmp_path/v2_drafts/<session>`
    pointing at `tmp_path/escape/` and asserts the call
    refuses.
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
- Incomplete draft → `ToolResponse.not_ready` with the
  `missing_fields` list.
- Validation failure (e.g. hash drift) →
  `ToolResponse.validation_failed` carrying the
  `ValidationIssue` list verbatim.
- `validate_schedule_spec` is called with `spec` ONLY (no
  `execution_plans` / no `registries` kwargs — round-2
  reviewer L386 / Q6). Pin via patching the function with
  a spy that records the call's args + kwargs.
- `schedule_draft_compile` does NOT write to the v2 DB.
  Pin: spy on `insert_schedule` (mocked import); assert
  zero calls.
- `schedule_draft_compile` does NOT delete the draft file
  (compile is non-destructive; phase 8 freeze + commit
  takes over).
- `schedule_draft_discard` deletes the draft file;
  idempotent (second call returns `ToolResponse.ok` with a
  "already absent" hint in `message`).
- `schedule_draft_list` returns the current draft ids in
  deterministic order; empty list when no drafts.
- No `schedule_draft_commit` symbol exists in
  `app/v2/authoring/compile.py` (round-2 reviewer L95 /
  Q5 — commit deferred). Pin via
  `assert not hasattr(compile_mod, "schedule_draft_commit")`.

### 5.6a `test_authoring_lifecycle_helper.py`

- `update_status_with_event` flips status AND appends the
  matching event in one TX. Pin: query schedules + events
  tables; both reflect the change after the call.
- Missing schedule_id → `ScheduleNotFoundError`.
- Helper rolls back on event-insert failure (simulate by
  monkey-patching `events.insert` to raise after the
  status update is staged). Pin: schedules row reverts to
  the pre-call status; no event row exists.
- Archive variant cancels every pending Run + appends a
  `run_cancelled` event per Run, all in one TX. Pin via
  seeding 3 pending Runs and asserting all 3 flip +
  3 `run_cancelled` events land after one call.
- Archive variant: a non-pending Run (running / succeeded
  / failed) is NOT touched by the cancel branch.

### 5.6b `test_authoring_lifecycle.py`

- Each lifecycle tool returns `ToolResponse.ok` on success;
  pin by querying schedules + events tables.
- Each lifecycle tool returns `ToolResponse.not_found` for
  a missing schedule.
- `schedule_pause` on an already-paused schedule emits
  another `schedule_paused` event (plan default per §9.2
  #15 — keep audit trail).
- `schedule_resume` on an archived schedule →
  `ToolResponse.validation_failed` with an issue naming the
  archived → PAUSED requirement (round-2 reviewer L442).
- `schedule_archive` cancels every pending Run + payload
  reports the cancelled count.
- `schedule_revive` flips archived → **PAUSED** (round-2
  reviewer L442). Subsequent `schedule_resume` then flips
  PAUSED → active.
- Phase-5 lifecycle hooks (`app.v2.runtime.lifecycle.*`)
  are NOT called by phase-7 tools — pin via patching the
  hook module's `on_schedule_paused/_archived/_resumed/_revised`
  and asserting zero calls across all four tools.
- `event_id_factory` is required (no default). Each event
  insert uses the factory; sequential calls produce
  distinct ids.

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
   `datetime.now`); path-traversal fence (slug regex +
   `Path.resolve().is_relative_to(base)`) pinned by
   dedicated tests passing `../`, `..\\`, absolute paths,
   and symlink-escape inputs.
4. Six setters (`schedule_draft_start` / `set_description` /
   `set_owner` / `set_cron` / `set_one_off` /
   `set_failure_policy`) validate the changed field
   immediately (Q8) and call `validate_schedule_spec(spec)`
   (no `execution_plans`, no `registries` — Q6) after
   mutation when the draft is complete; return
   `ToolResponse.not_ready` while still in flight.
5. `schedule_set_delivery` resolves the channel via the
   phase-6 cache; cache-absent + refresh-failure surfaces
   as `ToolResponse.cache_unavailable` (Q8 raise path
   closed). `cache_loader` / `cache_saver` / `slack_client`
   / `expected_owner_id` / `clock` all REQUIRED DI args (no
   env defaults — L365 / Q10).
6. `schedule_draft_compile` / `schedule_draft_discard` /
   `schedule_draft_list` ship. **No commit verb in phase
   7** (round-2 reviewer L95 / Q5 — deferred to phase 8
   alongside freeze + `schedule_created` event triplet).
7. `update_status_with_event` helper exists; flips status
   AND appends the matching EventLedger event in one
   transaction. Archive variant additionally cancels pending
   Runs (pending → cancelled + `run_cancelled` event per
   Run) in the same transaction. Rollback-on-error pinned.
8. Four lifecycle tools (`schedule_pause` / `_resume` /
   `_archive` / `_revive`) call the helper.
   `schedule_revive` transitions archived → **PAUSED**
   (round-2 reviewer L442); `schedule_resume` refuses when
   current status is archived. No phase-5 binding hooks
   called.
9. `AuthoringToolset` exposes every tool as a
   `FunctionTool` via `BaseToolset.get_tools`. Every tool
   has a `ToolDescriptor` registered in
   `app/v2/registry.py:ToolRegistry` with §5.4 metadata
   tags (Q7). NOT registered with any agent.
10. Phase guard `--diff v2-phase-6-complete` clean.
11. No v1 paths touched. `run_bot.py` untouched.
    `boot_runtime` untouched. `CoordinatorAgent` untouched.
12. No `datetime.now()` / `uuid.uuid4()` outside
    `_defaults.py`. AST pin on every authoring module.
13. Full v2 test suite passes (existing 1366 + phase-7
    adds); no regressions.
14. Annotated git tag `v2-phase-7-complete` created and
    pushed (workflow pre-approved per phase 6 kickoff).

---

## 8. Tag annotation

```
v2 phase 7 complete

Typed ADK authoring tools that build ScheduleSpec drafts
step-by-step. File-backed draft storage with atomic writes
and a path-traversal fence; field setters that validate the
changed field immediately and call validate_schedule_spec
on completion; channel-resolution path that funnels through
the phase-6 registry cache and surfaces cache-absent +
refresh-failure as ToolResponse.cache_unavailable; compile /
discard / list verbs (NO commit — deferred to phase 8 with
freeze); lifecycle tools (pause / resume / archive / revive)
over an atomic update_status_with_event helper that emits
the matching EventLedger event in the same transaction.
schedule_archive cancels every pending Run in the same TX;
schedule_revive transitions archived → PAUSED (admin
re-approves before resume). Toolset bundle in
app/v2/toolsets/authoring.py exposes every tool as a
FunctionTool and registers a ToolDescriptor per tool — but
the toolset is NOT mounted on any agent.

NO production agent mounting. CoordinatorAgent untouched;
boot_runtime untouched; no binding wiring; no env-derived
defaults. ExecutionPlan authoring (reasoning + source +
emit) deferred to phases 10 / 12. Dry-run + freeze +
commit + schedule_created event deferred to phase 8. v1
still owns production wakeup until phase 9 cutover.

Design: docs/CONTRACTS_V2_DESIGN.md §5.1, §5.5, §5.7,
        §11.4, §12 step 7
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

### 9.1.a Closed in round-2 reviewer

5. ~~Path-traversal on draft store.~~ **CLOSED** (L41 fix):
   slug regex `^[A-Za-z0-9_-]{1,128}$` + `Path.resolve()`
   guard rejecting any target whose resolved path is not
   relative to `base.resolve()`. Tests cover `../`, `..\\`,
   absolute paths, and symlink-escape.
6. ~~`schedule_draft_commit` lands rows before freeze.~~
   **CLOSED** (L95 / Q5): commit verb DEFERRED to phase 8
   alongside freeze + `schedule_created` event. Phase 7
   ships compile + discard + list only.
7. ~~Lifecycle status flips without events.~~ **CLOSED**
   (L109): new `update_status_with_event` helper flips
   status AND appends the matching EventLedger event in
   one transaction. Pinned by rollback-on-error tests.
8. ~~`schedule_revive` archived → active.~~ **CLOSED**
   (L442): archived → **PAUSED** instead. Admin must call
   `schedule_resume` separately. Two-step gate matches
   design §11.4.
9. ~~`schedule_archive` does not cancel pending Runs.~~
   **CLOSED** (L598): archive variant of the helper
   cancels every pending Run (pending → cancelled +
   `run_cancelled` event per Run) in the same TX as the
   status flip.
10. ~~`expected_owner_id` env-derived default.~~ **CLOSED**
    (L365 / Q10): REQUIRED DI on the toolset constructor
    and on `schedule_set_delivery`. Env wiring is phase-9
    cutover work.
11. ~~`validate_schedule_spec(spec, registry=...)` API.~~
    **CLOSED** (L386 / Q6): actual signature is
    `validate_schedule_spec(spec, *, execution_plans=None,
    registries=None)`. Phase 7 calls with both `None` —
    reminder-only flow. Future plan-backed flows DI both.
12. ~~`AuthoringToolset` registry integration.~~ **CLOSED**
    (Q7): phase 7 registers a `ToolDescriptor` in
    `app/v2/registry.py:ToolRegistry` for every authoring
    tool with the §5.4 metadata tags. Metadata only — no
    agent mount.
13. ~~Setter partial-validation policy.~~ **CLOSED** (Q8):
    setters validate the changed field immediately;
    `validate_schedule_spec(spec)` runs ONLY when the
    draft is complete.
14. ~~Cache-save side effect.~~ **CLOSED** (Q9): keep the
    auto-save after a successful refresh. `cache_saver` is
    an explicit DI parameter on `schedule_set_delivery` so
    tests stub it and phase-9 wires it.

### 9.2 Still open

15. **Idempotent-pause event behaviour.** Plan default
    (§3.6): calling `schedule_pause` on an already-paused
    schedule still appends a `schedule_paused` event for
    operator-action audit. Reviewer call: keep, or make
    the second call a no-op without an event? Default keeps
    the audit trail; switching to a no-op would need
    explicit operator action to disambiguate "intentional
    re-pause" from "accidental double-click".

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
