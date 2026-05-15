# Scheduler v2 — Phase 6 plan

Phase 5 shipped 2026-05-15 (tag `v2-phase-5-complete` on origin
at `4d028bb`). Phase 6 introduces the **registry cache** layer
for Slack channels and Google Drive / Docs items per design
`docs/CONTRACTS_V2_DESIGN.md` §5.7 and §12 step 6 (post-renumber).

This phase is intentionally **pure storage + lookup**. It does
not register an ADK tool, does not integrate with `boot_runtime`,
does not construct real Slack / Drive clients, and does not flip
v1 to v2. The cache is the substrate phase 7 (typed authoring
tools) will consume.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §5.7 ("Registry cache for
  channels / sheets / docs"), §5.3.7 (verbatim text), §12
  step 6 + §12.1 invariant 3.
- `docs/PHASE_5_PLAN.md` §10 (hard rules carried forward).

---

## 0. Design-doc amendment

None required. §5.7 already specifies the cache structure,
provenance, refresh modes, workspace-mismatch detection, and
no-cache-and-network-down semantics. If reviewer rounds surface
a gap, apply the amendment as a plan-revision commit and update
this §0 in that commit.

---

## 1. Scope statement

### In scope (phase 6)

1. **Cache schemas** — `app/v2/registry_cache/schemas.py`.
   Pydantic models for each cache file, every kind tagged with
   the provenance fields §5.7 lists (workspace_id /
   account_id, fetched_at, source, etag, items list).

2. **Cache paths** — `app/v2/registry_cache/paths.py`. Canonical
   on-disk location resolver. Default base
   `data/cache/registry/`; tests override via a base-path
   parameter or env var.

3. **Cache I/O** — `app/v2/registry_cache/loader.py`.
   `load_cache(kind, base=...) -> CacheFile | None` and
   `save_cache(kind, snapshot, base=...) -> None` with atomic
   tmp+rename write. Workspace-mismatch detection collapses
   a stale-workspace cache to `None` at load time (the caller
   then refreshes if a client is available, or surfaces a
   `NoCacheAndNetworkDown` once authoring tries to resolve).

4. **Refresh primitives** — `app/v2/registry_cache/refresh.py`.
   Per-kind functions that take a DI'd client and return a new
   `CacheFile` snapshot. No I/O to disk — caller decides whether
   to `save_cache` or just hand the snapshot to the resolver. The
   functions accept the client as a typed Protocol so the package
   does NOT import `slack_sdk` / `googleapiclient` at module
   level (deferred / `TYPE_CHECKING` only).

5. **Lookup helpers** — `app/v2/registry_cache/resolver.py`.
   `resolve_channel(id_or_name, cache)` /
   `resolve_sheet(spreadsheet_id, cache)` /
   `resolve_doc(document_id, cache)`. Each raises a
   subclass of `RegistryCacheError` on miss; no fallback to
   network from within `resolver.py` (network refresh is the
   authoring tool's call, not the resolver's).

6. **Error surface** — `app/v2/registry_cache/errors.py`.
   `RegistryCacheError` base + four subclasses:
   - `NoCacheAvailable` — cache file absent on disk.
   - `CacheMiss` — cache loaded but id not present.
   - `WorkspaceMismatch` — cached `workspace_id` /
     `account_id` differs from the expected one.
   - `NoCacheAndNetworkDown` — composite the authoring layer
     surfaces verbatim when boot couldn't load AND refresh
     can't fetch. Phase 6 ships the class; authoring uses it
     in phase 7.

7. **Stale-comment fix** — `app/v2/models/common.py` lines
   referencing "phase-3 registry" are updated to "phase-6
   registry cache" since this is the registry phase
   post-renumber. Comment-only; no schema change.

### Out of scope (phase 6)

- **ADK tool surface** for on-demand refresh — phase 7
  (typed authoring tools).
- **Lazy refresh at author time** — wiring lives in the
  authoring path; phase 7.
- **Background periodic refresh** — schedule + emit wiring
  is later; phase 6 ships the primitive only.
- **`boot_runtime` integration** — phase 5 surface stays
  untouched. Caches load lazily on first `resolve_*` call;
  no boot-time eager fetch.
- **Real Slack / Drive client construction** — phase 6
  accepts the client as DI. Production wiring lives outside
  this package.
- **`ChannelRef` / `SheetRef` Pydantic-level enum binding** —
  the resolver is shipped here; tightening the field types
  to validated enums lands in phase 7 when authoring tools
  start resolving on save.
- **v1 paths / `run_bot.py` / v1 scheduler** — design §12.1
  invariant 3 still in force; cutover at phase 9.
- **Source loaders** — phase 10 post-renumber.

---

## 2. New file paths

```
app/v2/registry_cache/__init__.py
app/v2/registry_cache/schemas.py
app/v2/registry_cache/paths.py
app/v2/registry_cache/loader.py
app/v2/registry_cache/refresh.py
app/v2/registry_cache/resolver.py
app/v2/registry_cache/errors.py

tests/v2/test_registry_cache_schemas.py
tests/v2/test_registry_cache_paths.py
tests/v2/test_registry_cache_loader.py
tests/v2/test_registry_cache_refresh.py
tests/v2/test_registry_cache_resolver.py
tests/v2/test_registry_cache_errors.py
```

Existing files touched:
- `app/v2/models/common.py` — comment-only update (lines
  citing "phase-3 registry").
- `.v2-current-phase` — bump 5 → 6 (per convention: marker
  tracks the highest phase whose plan exists).
- `scripts/check_phase_scope.py` — add `PHASE_ALLOWLIST[6]`.
- `docs/PHASE_6_PLAN.md` — this file.

---

## 3. Module APIs

### 3.1 `errors.py`

```python
class RegistryCacheError(Exception):
    """Base for every registry-cache failure."""


class NoCacheAvailable(RegistryCacheError):
    """Cache file does not exist on disk for the requested
    kind. Authoring layer's authoring-time refresh is expected
    to populate it. If the network is also unreachable, the
    authoring layer raises :class:`NoCacheAndNetworkDown`."""

    def __init__(self, kind: str, path: str) -> None:
        super().__init__(
            f"registry cache for kind={kind!r} missing at {path}"
        )
        self.kind = kind
        self.path = path


class CacheMiss(RegistryCacheError):
    """Cache loaded successfully but the requested id /
    name was not present. Distinct from
    :class:`NoCacheAvailable` so authoring tools can suggest
    a refresh rather than blame missing on-disk state."""

    def __init__(self, kind: str, lookup: str) -> None:
        super().__init__(
            f"id/name {lookup!r} not in cached {kind} list"
        )
        self.kind = kind
        self.lookup = lookup


class WorkspaceMismatch(RegistryCacheError):
    """Cached ``workspace_id`` / ``account_id`` differs from
    the expected one. The loader collapses such a cache to
    ``None``; this class exists so a refresh path that
    re-fetches and re-saves can surface the cause."""

    def __init__(self, kind: str, expected: str, found: str) -> None:
        super().__init__(
            f"{kind} cache workspace mismatch: "
            f"expected={expected!r}, found={found!r}"
        )
        self.kind = kind
        self.expected = expected
        self.found = found


class NoCacheAndNetworkDown(RegistryCacheError):
    """Authoring tried to resolve, no cache existed AND a
    fresh fetch failed. Phase 6 ships the class; the
    authoring layer (phase 7) is the call site that catches
    a ``NoCacheAvailable`` / refresh failure pair and
    re-raises this composite."""

    def __init__(self, kind: str, network_error: str) -> None:
        super().__init__(
            f"no cached {kind} listing and refresh failed: "
            f"{network_error}"
        )
        self.kind = kind
        self.network_error = network_error
```

### 3.2 `schemas.py`

```python
class SlackChannelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    is_archived: bool = False
    is_private: bool = False


class SlackChannelsCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["slack_channels"] = "slack_channels"
    workspace_id: str
    fetched_at: datetime
    source: str  # e.g. "slack.api.conversations.list"
    etag: Optional[str] = None
    channels: list[SlackChannelEntry]


class DriveItemEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    mime_type: str
    parent_id: Optional[str] = None


class GoogleDriveCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["google_drive_items"] = "google_drive_items"
    account_id: str
    fetched_at: datetime
    source: str  # e.g. "drive.api.files.list"
    etag: Optional[str] = None
    items: list[DriveItemEntry]


class GoogleDocsCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["google_docs_items"] = "google_docs_items"
    account_id: str
    fetched_at: datetime
    source: str  # e.g. "drive.api.files.list?mimeType=document"
    etag: Optional[str] = None
    docs: list[DriveItemEntry]


CacheKind = Literal[
    "slack_channels",
    "google_drive_items",
    "google_docs_items",
]
CacheFile = Union[
    SlackChannelsCache, GoogleDriveCache, GoogleDocsCache
]
```

Open question 6.1 — Drive items + Docs split vs single cache —
addressed in §9.2.

### 3.3 `paths.py`

```python
DEFAULT_CACHE_BASE = pathlib.Path("data/cache/registry")

_FILENAMES: dict[CacheKind, str] = {
    "slack_channels": "slack_channels.json",
    "google_drive_items": "google_drive_items.json",
    "google_docs_items": "google_docs_items.json",
}


def cache_path(kind: CacheKind, *, base: pathlib.Path | None = None) -> pathlib.Path:
    """Resolve the on-disk path for a cache file. ``base``
    defaults to :data:`DEFAULT_CACHE_BASE`; tests override."""
    root = base if base is not None else DEFAULT_CACHE_BASE
    return root / _FILENAMES[kind]
```

### 3.4 `loader.py`

```python
def load_cache(
    kind: CacheKind,
    *,
    expected_workspace_id: str | None = None,
    base: pathlib.Path | None = None,
) -> CacheFile | None:
    """Read + parse the cache for ``kind``.

    Returns ``None`` when:
    - the file is absent (no exception — the caller decides
      whether to refresh or raise),
    - ``expected_workspace_id`` is provided AND the cached
      workspace_id (or account_id for Drive/Docs kinds) does
      NOT match. Mismatch is logged; the on-disk file is left
      untouched so a manual review is possible.

    Parse errors (corrupt JSON, schema-invalid payload) raise
    ``RegistryCacheError`` so caller code does not silently
    fall back to "no cache".
    """


def save_cache(
    kind: CacheKind,
    snapshot: CacheFile,
    *,
    base: pathlib.Path | None = None,
) -> None:
    """Write ``snapshot`` to disk atomically via tmp + rename.

    Crash mid-write must leave either:
      (a) the previous good file untouched, or
      (b) a `.tmp.<pid>` orphan that does NOT shadow the
          canonical name.
    Pin via a test that opens a real tmp + rename pair.
    """
```

### 3.5 `refresh.py`

Per-kind functions; each takes a DI'd client Protocol. No
module-level import of `slack_sdk` / `googleapiclient`.

```python
class SlackChannelsClient(Protocol):
    def list_conversations(
        self, *, exclude_archived: bool = False
    ) -> Iterable[Mapping[str, Any]]:
        ...


def refresh_slack_channels(
    client: SlackChannelsClient,
    *,
    expected_workspace_id: str,
    clock=prod_clock,
) -> SlackChannelsCache:
    """Pull the current conversation list via ``client`` and
    build a fresh snapshot.

    NEVER writes to disk — the caller decides via
    :func:`save_cache`. Pure function over the client's
    iterable.
    """


class GoogleDriveClient(Protocol):
    def list_files(
        self, *, query: str | None = None
    ) -> Iterable[Mapping[str, Any]]:
        ...


def refresh_google_drive(
    client: GoogleDriveClient,
    *,
    expected_account_id: str,
    clock=prod_clock,
) -> GoogleDriveCache:
    ...


def refresh_google_docs(
    client: GoogleDriveClient,
    *,
    expected_account_id: str,
    clock=prod_clock,
) -> GoogleDocsCache:
    """Same client surface as ``refresh_google_drive``; the
    query filter (``mimeType='application/vnd.google-apps.document'``)
    is applied internally."""
```

### 3.6 `resolver.py`

```python
def resolve_channel(
    lookup: str,
    cache: SlackChannelsCache | None,
) -> SlackChannelEntry:
    """Find a channel by ``id`` OR by ``name`` (Slack ``#name``
    or bare ``name``). Raises :class:`NoCacheAvailable` when
    ``cache is None``, :class:`CacheMiss` when the lookup
    string matches neither id nor name in the loaded set."""


def resolve_sheet(
    spreadsheet_id: str,
    cache: GoogleDriveCache | None,
) -> DriveItemEntry:
    """Find a Drive item by id. Sheets-specific MIME filter
    is the caller's responsibility (the cache holds raw Drive
    items)."""


def resolve_doc(
    document_id: str,
    cache: GoogleDocsCache | None,
) -> DriveItemEntry:
    """Find a Docs item by id."""
```

### 3.7 `__init__.py`

Re-exports the public surface (cache models, kinds, load/save,
refresh, resolve, errors). Keeps imports concentrated so the
authoring layer can `from app.v2.registry_cache import …`.

---

## 4. Slice ordering + commit cadence

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan) | `docs/PHASE_6_PLAN.md` + `.v2-current-phase` 5 → 6 + `PHASE_ALLOWLIST[6]` | (none) |
| 1 | `errors.py` + `schemas.py` + `paths.py` | `test_registry_cache_errors.py` + `test_registry_cache_schemas.py` + `test_registry_cache_paths.py` |
| 2 | `loader.py` — load + atomic save + workspace mismatch invalidation | `test_registry_cache_loader.py` |
| 3 | `refresh.py` — DI'd client Protocols + pure refresh functions | `test_registry_cache_refresh.py` |
| 4 | `resolver.py` — id / name lookup + error surfacing | `test_registry_cache_resolver.py` |
| 5 | `__init__.py` re-exports + stale-comment fix in `app/v2/models/common.py` | smoke import test |
| closeout | acceptance + tag `v2-phase-6-complete` (gated on Sergey) | — |

Slice 5 is bundled because both items are trivial. If reviewer
prefers them separated, split into 5a (exports) and 5b (comment
fix); no design impact.

---

## 5. Test inventory

### 5.1 `test_registry_cache_errors.py`

- Each exception subclass exists, has the documented init
  signature, and produces a message containing the supplied
  fields.
- `NoCacheAndNetworkDown` carries both `kind` and
  `network_error` attributes.
- All four subclasses inherit from `RegistryCacheError`, which
  inherits from `Exception`.

### 5.2 `test_registry_cache_schemas.py`

- Each cache model rejects unknown fields (`extra="forbid"`).
- `Literal["slack_channels"]` discriminator is enforced.
- `fetched_at` accepts only tz-aware datetimes (mirrors the
  phase-4 / phase-5 invariant). Naive datetime → `ValidationError`.
- Round-trip through `model_dump_json` + `model_validate_json`
  preserves every field.
- Empty `channels` / `items` / `docs` list is valid (a freshly
  emptied workspace is legal).

### 5.3 `test_registry_cache_paths.py`

- `cache_path(kind)` returns the documented filename under
  `DEFAULT_CACHE_BASE`.
- `cache_path(kind, base=tmp_path)` returns the tmp-prefixed path.
- Unknown `kind` value raises `KeyError`.

### 5.4 `test_registry_cache_loader.py`

Load happy path:

- Save then load round-trips a `SlackChannelsCache` snapshot
  unchanged (including `etag=None`).
- Save then load round-trips `GoogleDriveCache` /
  `GoogleDocsCache`.

Missing file:

- `load_cache(kind, base=tmp_path)` with no file on disk
  returns `None` (not exception).

Workspace mismatch:

- Save with `workspace_id="T_OLD"`, load with
  `expected_workspace_id="T_NEW"` → returns `None`. Pin:
  the file is NOT deleted by the loader (manual review path).

Atomic write:

- During a simulated crash (the loader monkey-patches `os.rename`
  to raise after writing the tmp file), the previous good file
  is preserved unchanged; the tmp file remains for forensic
  inspection.
- A successful save leaves no `.tmp.*` artifacts in the cache
  directory.

Parse errors:

- Corrupt JSON → `RegistryCacheError`.
- JSON with an extra unknown field → `RegistryCacheError`
  (mirrors `extra="forbid"`).
- Schema-valid payload with wrong discriminator → `RegistryCacheError`.

### 5.5 `test_registry_cache_refresh.py`

Slack:

- A stub client returning two channels produces a
  `SlackChannelsCache` with two `SlackChannelEntry` rows in
  order; `fetched_at` matches the injected `clock`.
- Stub client returning an empty iterable → cache with empty
  `channels` list.
- `expected_workspace_id` is recorded as the snapshot's
  `workspace_id` (caller-supplied; client iteration does not
  override it). Pin so a future refactor that resolves
  workspace_id from the client payload is a deliberate change.
- The module does NOT import `slack_sdk` at module load:
  pin via `sys.modules` snapshot before + after importing
  `refresh`.

Drive / Docs:

- Stub client returning a Drive items list produces the
  matching `GoogleDriveCache` shape.
- `refresh_google_docs` filters by MIME inside the call
  (assert via spy on the client's `list_files(query=...)`
  parameter).
- The module does NOT import `googleapiclient` at module
  load (same `sys.modules` snapshot pattern).

Client errors propagate:

- Client iterable raising `RuntimeError("network down")` →
  `refresh_*` propagates the original exception unchanged.
  Phase 6 does NOT wrap into `NoCacheAndNetworkDown`; the
  authoring layer (phase 7) is the call site that catches +
  composes.

### 5.6 `test_registry_cache_resolver.py`

Channels:

- `resolve_channel("C012", cache)` returns the entry by id.
- `resolve_channel("#general", cache)` returns the entry by
  name (strips leading `#`).
- `resolve_channel("general", cache)` returns the entry by
  name (no `#`).
- `resolve_channel("missing", cache)` → `CacheMiss`.
- `resolve_channel("C012", cache=None)` → `NoCacheAvailable`.

Sheets / Docs:

- `resolve_sheet(id, cache)` returns the item.
- `resolve_sheet("nope", cache)` → `CacheMiss`.
- `resolve_sheet(..., cache=None)` → `NoCacheAvailable`.
- `resolve_doc` mirrors `resolve_sheet` against the docs cache.

### 5.7 Smoke

- `from app.v2.registry_cache import …` imports every public
  symbol named in `__init__.py:__all__`.
- The package does NOT import `slack_sdk` / `googleapiclient`
  / `requests` / `httpx` at module load (pin via inspection).
- No public callable in any phase-6 module dispatches to
  reasoning / emit / sub-agents / delegate / transfer / fire
  / claim / execute. Carried from phase 4-5.
- `app/v2/models/common.py` no longer references "phase-3" in
  the `ChannelRef` / `SheetRef` docstrings (grep-based pin).

---

## 6. CI guard checks

`scripts/check_phase_scope.py` gains:

```python
6: {
    "app/v2/",
    "tests/v2/",
    "scripts/check_phase_scope.py",
    "scripts/install_hooks.py",
    ".githooks/v2_phase_guard.sh",
    ".githooks/pre-commit",
    ".github/workflows/v2_phase_guard.yml",
    ".v2-current-phase",
    "docs/PHASE_6_PLAN.md",
    "docs/CONTRACTS_V2_DESIGN.md",
    ".docs_read_marker",
},
```

The v1-paths-forbidden rule (§12.1 invariant 3) carries
forward — phase 6 cannot touch `app/contracts/`,
`app/tasks.py`, `app/contracts/executor.py`,
`app/scheduler_instance.py`, `data/contracts/`. Cutover at
phase 9 per renumber.

Cross-cutting smoke checks added or carried:

- Phase-6 modules import no I/O libs (slack_sdk,
  googleapiclient, httpx, requests, urllib3, smtplib,
  subprocess) at module load. Protocol-typed DI keeps the
  package vendor-neutral.
- `_defaults.py` remains the ONLY runtime module whose smoke
  test asserts `uuid` / `datetime.now` ARE imported. Phase-6
  refresh functions take an injected `clock`; no
  `datetime.now()` call in `refresh.py`.
- No phase-6 module mutates `app/v2/runtime/` modules. The
  cache is a sibling subsystem.

---

## 7. Acceptance criteria for `v2-phase-6-complete`

1. Branch ahead of `v2-phase-5-complete` by N small commits,
   each scoped to one of the slices in §4.
2. `errors.py` ships four `RegistryCacheError` subclasses;
   smoke tests pin the inheritance + init signature.
3. `schemas.py` ships three cache models with
   `extra="forbid"` + tz-aware `fetched_at` enforcement.
4. `paths.py` resolves the documented filenames under
   `DEFAULT_CACHE_BASE = data/cache/registry/`.
5. `loader.py` round-trips every kind, returns `None` for
   missing file, returns `None` for workspace mismatch
   without deleting the file, raises `RegistryCacheError` on
   corrupt JSON / schema-invalid payload, writes atomically
   via tmp + rename.
6. `refresh.py` exposes one function per kind, each taking
   a Protocol-typed client + injected `clock`. The module
   does NOT import `slack_sdk` / `googleapiclient` at module
   load. Client errors propagate unchanged (no
   `NoCacheAndNetworkDown` wrap inside `refresh.py`).
7. `resolver.py` resolves channels by id or name (with the
   `#` prefix stripped), sheets / docs by id, raises
   `NoCacheAvailable` for `cache=None`, raises `CacheMiss`
   for valid cache + missing lookup.
8. `app/v2/models/common.py` docstrings no longer reference
   "phase-3" for the registry; the new docstring cites
   phase 6 + `app/v2/registry_cache/resolver.py`.
9. `__init__.py:__all__` covers every public symbol; smoke
   import test passes.
10. Phase guard clean against `v2-phase-5-complete`. No v1
    paths touched. `run_bot.py` untouched.
11. `boot_runtime` untouched (phase 6 ships no boot
    integration; lazy load on first `resolve_*` call).
12. Full v2 test suite passes (existing 1205 + phase-6 adds);
    no regressions.
13. Annotated git tag `v2-phase-6-complete` created and
    pushed (gated on explicit Sergey approval per workflow
    pattern Sergey pre-approved at phase 6 kickoff).

---

## 8. Tag annotation

```
v2 phase 6 complete

Registry cache for Slack channels / Google Drive items /
Google Docs items. Pure storage + lookup layer with
provenance (workspace_id / account_id, fetched_at, source,
etag), workspace-mismatch invalidation, atomic on-disk
writes, and DI'd refresh primitives that do NOT import
slack_sdk / googleapiclient at module load.

NO production wiring. ADK tool surface lands in phase 7
(typed authoring tools); boot integration stays deferred;
v1 still owns production wakeup until phase 9 cutover
(OneOffReminder end-to-end per design §12.1 invariant 3
post-renumber).

Design: docs/CONTRACTS_V2_DESIGN.md §5.7, §12 step 6
Plan:   docs/PHASE_6_PLAN.md
```

---

## 9. Open questions

### 9.1 Closed in this revision

1. ~~Cache base directory.~~ **CLOSED.** Design §5.7 example
   uses `data/cache/...`; phase 6 nests under
   `data/cache/registry/` to leave room for future cache
   subsystems without clashing.
2. ~~Boot-time eager fetch.~~ **CLOSED.** §5.7 explicitly says
   "Boot does NOT block on Slack/Drive reachability"; phase 6
   keeps `boot_runtime` untouched and uses lazy load.

### 9.2 Still open

3. **Drive items + Docs split vs unified.** §5.7 lists
   `slack_channels.json` only and is silent on Drive shape.
   Phase 6 default splits Drive and Docs into two cache files
   because their authoring paths are distinct (sheets / docs).
   A unified `google_drive_resources.json` with a
   discriminator field is also viable. Reviewer call.

4. **`etag` semantics.** Design §5.7 includes an `etag` field
   but does not pin a producer. Slack `conversations.list`
   has no first-class etag; Drive returns one. Phase 6 keeps
   the field optional. Reviewer: should phase-6 refresh
   compute a content hash and stuff it into `etag` for kinds
   with no native value? Default: leave `None` for Slack.

5. **Stale-warning TTL.** §5.7 says "logs stale warning" but
   no number. Phase 6 default: no TTL check inside the
   package (caller compares `fetched_at` to `clock()` and
   logs at its discretion). Add a helper if reviewer wants a
   standardised "older than 24 h → warn" predicate.

6. **`name` collisions for channels.** Slack channel names
   are unique within a workspace at any moment but can be
   reused after deletion. The resolver returns the first
   match; that's defensible while the cache mirrors a
   single workspace. Pin in tests so a future "raise on
   ambiguity" change surfaces.

7. **Workspace-mismatch policy.** Loader returns `None` and
   leaves the file. Alternative: rename the file with a
   `.mismatch-<ts>.json` suffix so a refresh can re-save
   without colliding. Default: leave the file; reviewer
   decides if the rename is worth the complexity.

8. **Authoring-layer composite.** Phase 7 owns the
   `NoCacheAndNetworkDown` raise path. Phase 6 ships the
   class. Reviewer to confirm boundary is correct.

---

## 10. Hard rules (carried forward)

Same as phase 5 plan §10. Restated for self-containment:

1. No push without explicit Sergey approval.
2. No edits to phase-1 through phase-5 plan docs without a
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
10. Every runtime helper still takes injected clock + id
    factories; `_defaults.py` is the ONLY module that wires
    them to wall clock + uuid4. Phase-6 refresh functions
    take a `clock` parameter following this rule even though
    they live outside `app/v2/runtime/`.
