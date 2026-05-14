# Scheduler v2 — Phase 2 plan

Phase 1 shipped 2026-05-14 (tag `v2-phase-1-complete`). Phase 2
narrows the next slice of `docs/CONTRACTS_V2_DESIGN.md` §12 step 2:

> Adapter / source / loader input-output Pydantic models +
> tool metadata tags. Per-adapter request/response shapes. No
> runtime use yet.

Read this with the design contract (§5.4 — tool tags; §5.3 — source
loaders; §5.5 — single validation entry point; §6.2 — emit adapters)
and Phase 1's plan for the workflow vocabulary.

---

## 1. Scope statement

### In scope (phase 2)

1. **Tool capability tags** — the canonical enum from design §5.4
   (`read_external`, `write_external`, `send_message`,
   `filesystem_write`, `privileged`, `costly`, `uses_oauth`).
2. **ToolDescriptor** — registry shape for any tool / loader /
   adapter the agent may call. Carries name + tags + description.
3. **Source contracts** — `SourceDescriptor`,
   `SourceInputContract`, `SourceOutputContract`. Defines what a
   source loader takes in and must return out (the bytes go to
   disk; the metadata matches `SourceSnapshotMetadata` from
   phase 1).
4. **Emit contracts** — `EmitDescriptor`, `EmitInputContract`,
   `EmitOutputContract`. Defines emit-adapter request shape
   including idempotency token, and the response shape including
   `delivered`, destination-side id, and structured error.
5. **Policy helper functions** — pure functions over a tag set:
   `is_blocked_by_read_only_reasoning(tags)`,
   `requires_admin_approval(tags)`,
   `is_costly(tags)`, `requires_oauth(tags)`. These are the
   shape the runtime guard will consume in a later phase.
6. **Drift guards** — tests that iterate every canonical tag
   value and every SelectionMethod and assert the contract
   accepts them, mirroring the phase-1 DDL drift guards.

### Out of scope (phase 2)

- Any worker / claim / runtime / wakeup code.
- APScheduler edits.
- Any concrete adapter implementation (no real Slack / Drive /
  Telegram bytes flowing through the descriptors yet).
- Live source caching, retention, fallback, drift behavior —
  that lives in phase 9.
- V1 scheduler edits (`app/contracts/`, `app/tasks.py`,
  `app/contracts/executor.py`, `app/scheduler_instance.py`,
  `data/contracts/`).
- Coordinator / sub-agent instruction edits.
- Any tool wiring into ADK.

---

## 2. New file paths

```
app/v2/
  tool_tags.py                # ToolCapabilityTag enum + helpers
  descriptors/
    __init__.py
    tool.py                   # ToolDescriptor (registry shape)
    source.py                 # SourceDescriptor + Input/Output
    emit.py                   # EmitDescriptor + Input/Output

tests/v2/
  test_tool_tags.py           # tag enum coverage + policy helpers
  test_descriptors_tool.py    # ToolDescriptor validation
  test_descriptors_source.py  # SourceDescriptor + I/O contracts
  test_descriptors_emit.py    # EmitDescriptor + I/O contracts

docs/PHASE_2_PLAN.md          # this file
```

Existing files touched (only for phase scaffolding):

- `scripts/check_phase_scope.py` — widen `PHASE_ALLOWLIST[2]`
  to mirror phase 1's machinery surface (plus PHASE_2_PLAN.md).
- `.v2-current-phase` — bumped from `1` to `2`.

Everything else is additive.

---

## 3. Tool capability tags

The seven canonical values per design §5.4:

```python
class ToolCapabilityTag(str, Enum):
    READ_EXTERNAL = "read_external"
    WRITE_EXTERNAL = "write_external"
    SEND_MESSAGE = "send_message"
    FILESYSTEM_WRITE = "filesystem_write"
    PRIVILEGED = "privileged"
    COSTLY = "costly"
    USES_OAUTH = "uses_oauth"
```

Policy helpers (pure functions, no I/O):

| Helper | Returns True iff |
|---|---|
| `is_blocked_by_read_only_reasoning(tags)` | tags ∩ {`write_external`, `send_message`, `filesystem_write`, `privileged`} ≠ ∅ |
| `requires_admin_approval(tags)` | tags ∩ {`privileged`, `costly`, `filesystem_write`} ≠ ∅ (design §5.9 CustomFlow friction triggers) |
| `is_costly(tags)` | `costly` ∈ tags |
| `requires_oauth(tags)` | `uses_oauth` ∈ tags |
| `is_user_facing(tags)` | `send_message` ∈ tags |

`is_costly` and `requires_admin_approval` overlap on `COSTLY`
deliberately — they address different concerns (cost warning vs.
authoring-time approval gate). Both stay independently callable.

Default tag for unknown tools is `write_external` (fail-safe per
design §5.4 final line). Future ToolDescriptors registered without
an explicit tag set are rejected at construction — fail-loud, not
fail-fast-with-defaults.

---

## 4. Descriptor models

### 4.1 ToolDescriptor

```
name:        str (snake_case)
description: str (min 8 chars)
tags:        set[ToolCapabilityTag] (non-empty)
module:      str (importable path, e.g. "app.v2.adapters.slack")
```

Pure contract. No executor reference (that's runtime). The
registry lookup tooling lands in a later phase.

### 4.2 SourceDescriptor

```
id:                           str (snake_case, e.g. "source_drive_file")
description:                  str
tags:                         set[ToolCapabilityTag]
                              MUST include READ_EXTERNAL
supports_versioning:          bool
supported_selection_methods:  list[SelectionMethod] (non-empty)
```

Source loaders are always read-only by the source layer.
Validator forbids `write_external | send_message | filesystem_write`.

### 4.3 SourceInputContract (base)

Fields every source-load request includes:

```
run_id:           str
source_id:        str (input_id from ExecutionPlan)
as_of_datetime:   datetime | None
```

Source-specific subclasses add fields (e.g. `drive_file_id`,
`sheets_url`); the base is what the runtime guard checks against
when validating request shape.

### 4.4 SourceOutputContract

Subset of `SourceSnapshotMetadata` from phase 1 — only the fields
the adapter alone knows. The runtime merges the matching
`SourceInputContract`'s `run_id` + `source_id` with this response
to assemble the storage row (`SourceSnapshotMetadata`). Adapters
never echo their inputs back here.

```
content_hash:      str  ^sha256:[0-9a-f]{64}$
content_path:      str  (relative, no '..')
content_size:      int >= 0
fetched_at:        datetime
source_kind:       str
source_version:    str | None
selection_method:  SelectionMethod
```

Documented invariant pinned in tests:
`SourceSnapshotMetadata.fields == SourceOutputContract.fields ∪ {run_id, source_id}`.

### 4.5 EmitDescriptor

```
id:                     str (snake_case, e.g. "slack_post_message")
description:            str
tags:                   set[ToolCapabilityTag]
                        MUST include SEND_MESSAGE or WRITE_EXTERNAL
target_kind:            str (slack / telegram / drive / sheet / etc.)
supports_native_dedup:  bool
                        True iff the destination accepts an
                        idempotency token (e.g. Slack
                        client_msg_id, Drive request_id).
```

### 4.6 EmitInputContract (base)

```
schedule_id:      str
root_run_id:      str
emit_id:          str
idempotency_key:  str  exact value of compute_idempotency_key(...)
payload:          dict[str, Any] | str
```

`idempotency_key` is validated by re-deriving the value from
schedule_id + root_run_id + emit_id and comparing — guards
against callers passing a stale or hand-rolled key.

### 4.7 EmitOutputContract

```
delivered:         bool
destination_id:    str | None  (e.g. slack message ts; null on failure)
attempted_at:      datetime
error:             str | None
```

`delivered ⇔ error is None` — the response carries the same fact
two ways so both adapter authors and audit-log readers stay
consistent.

### 4.8 Registry layer (slice 2)

Three typed registries hold descriptors at boot. Metadata only —
no dispatch, no execution.

```
ToolRegistry    keyed by ToolDescriptor.name
SourceRegistry  keyed by SourceDescriptor.id
EmitRegistry    keyed by EmitDescriptor.id
```

Common surface (`_BaseRegistry[D]`):

| Method | Behavior |
|---|---|
| `register(descriptor)` | TypeError if wrong descriptor type; `_validate(descriptor)` hook for subclass belt checks; `DuplicateDescriptorError` on key collision; insert. |
| `lookup(key)` | Descriptor or `None`. |
| `require(key)` | Descriptor or registry-specific `Unknown*Error`. |
| `__contains__` / `__iter__` / `__len__` / `keys()` / `clear()` | Standard. `clear()` for test isolation only. |

`ToolRegistry` adds `tags_for(name)`: returns the descriptor's
tags if registered, else `FAIL_SAFE_UNKNOWN_TAGS =
frozenset({WRITE_EXTERNAL})` per design §5.4 final paragraph.
This is the read-only-reasoning gate's fail-safe — it is NOT
the admin-approval gate, which is a separate "previously-unused
adapter" runtime check.

`SourceRegistry._validate` re-asserts the read-only invariant
(must include `READ_EXTERNAL`; must NOT include `WRITE_EXTERNAL` /
`SEND_MESSAGE` / `FILESYSTEM_WRITE`). Belt against
`model_construct` bypass — Pydantic already enforces this at
normal construction.

`EmitRegistry._validate` re-asserts that the descriptor carries
at least one of `SEND_MESSAGE` or `WRITE_EXTERNAL`.

Module-level singletons (`TOOLS`, `SOURCES`, `EMITS`) exist so
production code has a stable boot-time target. Tests construct
fresh instances rather than mutating the singletons.

Explicitly absent from the surface (verified by a smoke test):
no `dispatch`, `invoke`, `call`, `execute`, `run`, `send`
methods on any registry. No I/O imports in the module. Phase 2
is metadata only.

---

## 5. Test inventory

### 5.1 `tests/v2/test_tool_tags.py`

- Every canonical tag accepted by `ToolCapabilityTag`.
- Unknown / mistyped tag values rejected.
- Drift guard: every value in the enum has a string equal to its
  Python identifier lowercased — prevents accidental rename.
- Each policy helper covers its canonical input set + a
  negative case.
- Empty tag set: every helper returns False.

### 5.2 `tests/v2/test_descriptors_tool.py`

- Snake_case name validator (reject CamelCase, kebab-case, empty).
- Description min length 8.
- `tags` is non-empty (reject `set()`).
- Unknown tag rejected.
- `module` non-empty.
- `extra="forbid"` (round-trip with an extra field raises).

### 5.3 `tests/v2/test_descriptors_source.py`

**SourceDescriptor:**
- Requires `READ_EXTERNAL` in tags (reject without).
- Rejects descriptors carrying `WRITE_EXTERNAL | SEND_MESSAGE | FILESYSTEM_WRITE` (the source layer never writes).
- `supported_selection_methods` non-empty.
- Drift guard: each `SelectionMethod` value accepted.

**SourceInputContract:**
- Required fields present.
- `as_of_datetime` is optional.
- Subclassing supported (add a child class with an extra
  field; parent contract still validates).

**SourceOutputContract:**
- `content_hash` regex matches the phase-1 SourceSnapshotMetadata
  rule (sha256 prefix, 64 lowercase hex).
- `content_path` rejects absolute paths + `..`.
- `content_size >= 0`.
- `selection_method` constrained to enum.

### 5.4 `tests/v2/test_descriptors_emit.py`

**EmitDescriptor:**
- Requires `SEND_MESSAGE` or `WRITE_EXTERNAL` in tags (reject
  without either).
- `target_kind` non-empty.
- `supports_native_dedup` boolean.

**EmitInputContract:**
- `idempotency_key` MUST equal `compute_idempotency_key(...)`
  applied to the same triple — fail loud on mismatch.
- `payload` accepts dict or str.

**EmitOutputContract:**
- `delivered=True` requires `error is None`.
- `delivered=False` requires `error` non-empty.
- `attempted_at` is timezone-aware (UTC enforced like other v2
  datetime fields).

### 5.5 `tests/v2/test_registry.py` (slice 2)

For each registry:
- Register + lookup round-trip; `require` raises on unknown
  with the registry-specific error subclass.
- Duplicate registration raises `DuplicateDescriptorError`.
- Wrong descriptor type raises `TypeError` (independent of any
  belt checks).
- Membership / iteration / `keys()` / `clear()` behave as
  specified.

`ToolRegistry`:
- `tags_for(known)` returns the descriptor's tags.
- `tags_for(unknown)` returns `FAIL_SAFE_UNKNOWN_TAGS`.
- The fail-safe composes correctly: `is_blocked_by_read_only_reasoning(fail_safe) is True`; `requires_admin_approval(fail_safe) is False`.

`SourceRegistry`:
- Belt rejects `model_construct`-built descriptor with missing
  `READ_EXTERNAL` (matches Pydantic error).
- Belt rejects `model_construct`-built descriptor carrying any
  of `WRITE_EXTERNAL` / `SEND_MESSAGE` / `FILESYSTEM_WRITE`.

`EmitRegistry`:
- Belt rejects `model_construct`-built descriptor that lacks both
  `SEND_MESSAGE` and `WRITE_EXTERNAL`.

No-execution invariant:
- `app.v2.registry` imports no I/O libraries (httpx, requests,
  slack_sdk, googleapiclient, etc.).
- No public method named `dispatch`, `invoke`, `call`,
  `execute`, `run`, or `send` exists on any registry class.

---

## 6. CI guard adjustments

`PHASE_ALLOWLIST[2]` widens from the prior placeholder
(`{"app/v2/", "tests/v2/"}`) to mirror phase 1's machinery
surface — same set of items but with `docs/PHASE_2_PLAN.md` in
place of phase 1's plan doc:

```python
2: {
    "app/v2/",
    "tests/v2/",
    "scripts/check_phase_scope.py",
    "scripts/install_hooks.py",
    ".githooks/v2_phase_guard.sh",
    ".githooks/pre-commit",
    ".github/workflows/v2_phase_guard.yml",
    ".v2-current-phase",
    "docs/PHASE_2_PLAN.md",
    "docs/CONTRACTS_V2_DESIGN.md",
    ".docs_read_marker",
}
```

`docs/PHASE_1_PLAN.md` is deliberately omitted — phase 1 is
shipped. If a phase-1 doc fix is genuinely needed during phase 2,
use the `PHASE_OVERRIDE:` mechanism with explicit ack.

---

## 7. Commit ordering

Test-first invariant per design §11.3 still applies — every code
commit ships with its tests in the same commit.

| # | scope | files | status |
|---|---|---|---|
| 1 | phase bump + plan + allowlist widening + tags enum + descriptors + tests | this file, `.v2-current-phase`, `scripts/check_phase_scope.py`, the seven files in §2 | **shipped** (ae7b000 + reviewer fix 58b9bd6) |
| 2 | registry layer (typed registries for tool / source / emit, fail-safe tag lookup, belt-checks for `model_construct` bypass) + tests | `app/v2/registry.py`, `tests/v2/test_registry.py` | **in flight** |
| 3 | … to be determined by reviewer / Sergey after slice 2 lands | | pending |

The "first slice" was intentionally larger than phase 1's first
commit: it included the phase scaffolding (plan + bump + allowlist)
AND the canonical metadata enum + descriptors + tests. Slice 2
is a thinner add — just the registry layer that stores
descriptors without executing them. Subsequent phase-2 slices
will add more granular contracts (per-source / per-emit
subclasses) once reviewer signs off on the base shapes.

---

## 8. Acceptance criteria for phase 2 completion

Phase 2 is complete when ALL true:

1. All files in §2 exist.
2. `uv run python -m pytest tests/v2/ --tb=short -q` passes with
   zero failures.
3. `uv run python -m pytest tests/` (full suite) still passes.
4. `uv run python scripts/check_phase_scope.py --diff v2-phase-1-complete` reports no violations.
5. Every `ToolCapabilityTag` value has a passing test.
6. Every `SelectionMethod` value has a passing test in the
   SourceOutputContract drift guard.
7. `EmitInputContract`'s idempotency-key validator round-trips
   against `compute_idempotency_key` from phase 1 — i.e. it
   does not re-implement the format.
8. No file under `app/contracts/`, `app/tasks.py`,
   `app/contracts/executor.py`, `app/scheduler_instance.py`, or
   `data/contracts/` is modified during phase 2.
9. CI workflow `.github/workflows/v2_phase_guard.yml` still passes.
10. Annotated git tag `v2-phase-2-complete` created and pushed.

---

## 9. Phase-2 tag annotation

```
v2 phase 2 complete

Adds contract surface for tools, source loaders, and emit
adapters: ToolCapabilityTag enum, ToolDescriptor,
SourceDescriptor + Input/Output contracts, EmitDescriptor +
Input/Output contracts, policy helpers.

No runtime adapter implementation. No worker code. v1 paths
untouched.

Design: docs/CONTRACTS_V2_DESIGN.md §5.3, §5.4, §12 step 2
Plan:   docs/PHASE_2_PLAN.md
```

---

## 10. Open questions

None at slice 1 start. Will be filed here as they surface.
