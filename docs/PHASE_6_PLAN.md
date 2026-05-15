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
   `RegistryCacheError` base + five subclasses (round-2
   stale-count fix — previously read "four"):
   - `NoCacheAvailable` — cache file absent on disk OR the
     caller passed `cache=None` into a resolver.
   - `CacheMiss` — cache loaded but id not present.
   - `WorkspaceMismatch` — cached `workspace_id` /
     `account_id` differs from the expected one.
   - `NoCacheAndNetworkDown` — composite the authoring layer
     surfaces verbatim when boot couldn't load AND refresh
     can't fetch. Phase 6 ships the class; authoring uses it
     in phase 7.
   - `ChannelAmbiguous` — name lookup matched multiple
     active Slack channels. IDs stay canonical; authoring
     re-prompts.

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
    """Cache for the requested kind is not available.

    Two call paths surface this (round-2 reviewer L154 fix):

    - ``loader.load_cache`` callers that want to raise rather
      than treat ``None`` as "absent" pass the on-disk path
      via ``path`` so the message is forensically useful.
    - ``resolver.resolve_*`` raises with ``path=None`` when
      the caller passed ``cache=None``; the resolver has no
      file path. The message degrades to ``not provided``
      so reviewers reading a log can tell the two cases
      apart without diffing the call site.
    """

    def __init__(self, kind: str, path: str | None = None) -> None:
        if path is None:
            super().__init__(
                f"registry cache for kind={kind!r} not provided"
            )
        else:
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


class ChannelAmbiguous(RegistryCacheError):
    """Lookup by name matched more than one ACTIVE Slack
    channel. IDs stay canonical; authoring layer should
    re-prompt the user for the explicit id. Phase 6 raises
    this; phase 7 owns the user-facing rewording."""

    def __init__(self, name: str, candidate_ids: list[str]) -> None:
        super().__init__(
            f"channel name {name!r} matches {len(candidate_ids)} "
            f"active entries: {candidate_ids!r}"
        )
        self.name = name
        self.candidate_ids = candidate_ids
```

### 3.2 `schemas.py`

```python
# Shared UTC validator — every cache's ``fetched_at``
# MUST be a UTC datetime (tz-aware with utcoffset == 0).
# Mirrors the runtime invariant from phases 4-5 (naive
# datetime is a bug, and non-UTC fetched_at would break
# the is_stale comparison cross-tz). Round-2 reviewer L229
# fix: previously accepted any tz-aware offset; tightened
# to UTC-only.
def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "registry_cache fetched_at must be tz-aware UTC "
            "(got naive datetime)"
        )
    if value.utcoffset() != timedelta(0):
        raise ValueError(
            "registry_cache fetched_at must be UTC "
            f"(got utcoffset={value.utcoffset()!r}); convert via "
            "value.astimezone(timezone.utc) before persistence"
        )
    return value


# Common owner-id naming: ``owner_id`` covers both Slack
# ``workspace_id`` and Google ``account_id`` so call-site
# parameters stay uniform across kinds (reviewer round-1
# rename per L295 finding). The class attribute keeps the
# kind-specific label inside the model for human readers
# of the on-disk file.


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

    @field_validator("fetched_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @property
    def owner_id(self) -> str:
        """Uniform name across kinds — see L295 rename."""
        return self.workspace_id


class GoogleSheetsEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    mime_type: Literal[
        "application/vnd.google-apps.spreadsheet"
    ] = "application/vnd.google-apps.spreadsheet"
    parent_id: Optional[str] = None


class GoogleSheetsCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["google_sheets_items"] = "google_sheets_items"
    account_id: str
    fetched_at: datetime
    source: str  # e.g. "drive.api.files.list?mimeType=spreadsheet"
    etag: Optional[str] = None
    items: list[GoogleSheetsEntry]

    @field_validator("fetched_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @property
    def owner_id(self) -> str:
        return self.account_id


class GoogleDocsEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    mime_type: Literal[
        "application/vnd.google-apps.document"
    ] = "application/vnd.google-apps.document"
    parent_id: Optional[str] = None


class GoogleDocsCache(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["google_docs_items"] = "google_docs_items"
    account_id: str
    fetched_at: datetime
    source: str  # e.g. "drive.api.files.list?mimeType=document"
    etag: Optional[str] = None
    docs: list[GoogleDocsEntry]

    @field_validator("fetched_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return _require_utc(v)

    @property
    def owner_id(self) -> str:
        return self.account_id


CacheKind = Literal[
    "slack_channels",
    "google_sheets_items",
    "google_docs_items",
]
CacheFile = Union[
    SlackChannelsCache, GoogleSheetsCache, GoogleDocsCache
]
```

**Reviewer fixes applied here:**

- L223 + L229 — `fetched_at` now carries a `field_validator`
  that rejects naive datetime AND non-UTC offsets. Shared
  `_require_utc` helper is the single producer of both
  error messages (naive vs. non-UTC) so a future change
  touches one site. `utcoffset() != timedelta(0)` → raise;
  the cache invariant is UTC-only (not just "tz-aware"),
  matching the runtime invariant from phases 4-5 and
  keeping `is_stale` arithmetic safe.
- L398 — `GoogleDriveCache` is gone. `GoogleSheetsCache`
  holds `GoogleSheetsEntry` whose `mime_type` is a
  ``Literal["application/vnd.google-apps.spreadsheet"]``;
  `GoogleDocsCache` holds `GoogleDocsEntry` with the docs
  MIME literal. MIME enforcement is intrinsic to the model;
  the resolver can no longer accidentally accept a docs
  payload as a sheet.
- L295 — kinds expose a uniform `owner_id` property; the
  loader / refresh / resolver call sites switch to the
  uniform name (see §3.4–§3.6 below).
- Per Q3 — split is Sheets + Docs (NOT Drive + Docs).
- Per Q4 — `etag` stays optional; Slack stays `None` (refresh
  never computes a hash).

### 3.3 `paths.py`

```python
DEFAULT_CACHE_BASE = pathlib.Path("data/cache/registry")

_FILENAMES: dict[CacheKind, str] = {
    "slack_channels": "slack_channels.json",
    "google_sheets_items": "google_sheets_items.json",
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
    expected_owner_id: str | None = None,
    base: pathlib.Path | None = None,
) -> CacheFile | None:
    """Read + parse the cache for ``kind``.

    Returns ``None`` when:
    - the file is absent (no exception — the caller decides
      whether to refresh or raise),
    - ``expected_owner_id`` is provided AND the cached
      ``owner_id`` property (workspace_id for slack,
      account_id for sheets/docs) does NOT match. Mismatch is
      logged; the on-disk file is left untouched so a manual
      review is possible (per Q7 — no rename).

    Parse errors (corrupt JSON, schema-invalid payload, kind
    discriminator mismatch) raise ``RegistryCacheError`` so
    caller code does not silently fall back to "no cache".
    """


def save_cache(
    kind: CacheKind,
    snapshot: CacheFile,
    *,
    base: pathlib.Path | None = None,
) -> None:
    """Write ``snapshot`` to disk atomically via tmp + rename.

    Reviewer L314 fix: if ``snapshot.kind != kind`` raises
    ``ValueError`` BEFORE any I/O — phase 6 must never persist
    a docs payload at the slack path (or any other
    kind-channel pair). The check is the first line of the
    function so callers see a hard fail rather than a silent
    corrupting write.

    Crash mid-write must leave either:
      (a) the previous good file untouched, or
      (b) a `.tmp.<pid>` orphan that does NOT shadow the
          canonical name.
    Pin via a test that opens a real tmp + rename pair.
    """


def is_stale(
    cache: CacheFile,
    *,
    now: datetime,
    ttl: timedelta = timedelta(hours=24),
) -> bool:
    """Pure freshness predicate (per Q5 answer). Returns
    True when ``now - cache.fetched_at > ttl``. Phase 6
    ships the helper but does NOT log on staleness — the
    caller (phase 7 authoring) decides whether to warn,
    refresh, or proceed. ``now`` is the caller's clock
    output; the helper itself does not import a clock to
    keep `_defaults.py` the sole `datetime.now` site."""
```

### 3.5 `refresh.py`

Per-kind functions; each takes a DI'd client Protocol. No
module-level import of `slack_sdk` / `googleapiclient`. No
module-level import of `app.v2.runtime._defaults` either —
reviewer L347 fix: `clock` is a REQUIRED keyword-only
parameter with no default, so the package stays decoupled
from the runtime layer. Production callers (phase 7) pass
`prod_clock`; tests pass a synthetic clock.

**Archive policy (round-3 reviewer):** the cache mirrors
the FULL ``conversations.list`` result — archived + active
entries land in :attr:`SlackChannelsCache.channels`
verbatim. The resolver filters by ``is_archived`` at lookup
time via :func:`resolve_channel`'s ``include_archived``
keyword (default ``False`` per Q6). The Protocol therefore
exposes no archive-filter knob; any such filter belongs to
the production wrapper or the resolver, not the cache layer.

```python
class SlackChannelsClient(Protocol):
    def list_conversations(self) -> Iterable[Mapping[str, Any]]:
        ...


def refresh_slack_channels(
    client: SlackChannelsClient,
    *,
    expected_owner_id: str,
    clock: Callable[[], datetime],
) -> SlackChannelsCache:
    """Pull the current conversation list via ``client`` and
    build a fresh snapshot.

    ``expected_owner_id`` is recorded as the snapshot's
    ``workspace_id``. ``clock()`` populates ``fetched_at``;
    the value must be UTC-only (pinned by the schema
    validator).

    The cache mirrors the FULL conversations.list result —
    archived + active. See the SlackChannelsClient docstring
    for the archive policy.

    NEVER writes to disk — the caller decides via
    :func:`save_cache`. Pure function over the client's
    iterable.
    """


class GoogleDriveClient(Protocol):
    def list_files(
        self, *, query: str | None = None
    ) -> Iterable[Mapping[str, Any]]:
        ...


def refresh_google_sheets(
    client: GoogleDriveClient,
    *,
    expected_owner_id: str,
    clock: Callable[[], datetime],
) -> GoogleSheetsCache:
    """Filters by
    ``mimeType='application/vnd.google-apps.spreadsheet'``
    inside the call. The schema's MIME literal will reject
    any cross-type payload at validation time, so a
    forgotten filter surfaces as a schema error during the
    next save_cache (defence in depth)."""


def refresh_google_docs(
    client: GoogleDriveClient,
    *,
    expected_owner_id: str,
    clock: Callable[[], datetime],
) -> GoogleDocsCache:
    """Filters by
    ``mimeType='application/vnd.google-apps.document'``
    inside the call. Same defence-in-depth pattern as
    :func:`refresh_google_sheets`."""
```

### 3.6 `resolver.py`

```python
def resolve_channel(
    lookup: str,
    cache: SlackChannelsCache | None,
    *,
    include_archived: bool = False,
) -> SlackChannelEntry:
    """Find a channel by ``id`` OR by ``name`` (Slack ``#name``
    or bare ``name``).

    Raises:
    - :class:`NoCacheAvailable` when ``cache is None``.
    - :class:`CacheMiss` when the lookup matches neither id
      nor name in the active subset.
    - :class:`ChannelAmbiguous` when ``lookup`` is a NAME (not
      an id) AND the active subset contains more than one
      matching entry (reviewer L714 fix per Q6 — IDs stay
      canonical; ambiguous names re-prompt).

    ``include_archived`` defaults to False so archived
    channels are filtered out before the lookup runs. Pass
    ``include_archived=True`` when the caller explicitly
    needs an archive listing.
    """


def resolve_sheet(
    spreadsheet_id: str,
    cache: GoogleSheetsCache | None,
) -> GoogleSheetsEntry:
    """Find a sheets entry by id. MIME enforcement is built
    into the cache schema (per reviewer L398 fix); a docs
    payload could never reach this resolver because the
    cache class itself would reject it during load."""


def resolve_doc(
    document_id: str,
    cache: GoogleDocsCache | None,
) -> GoogleDocsEntry:
    """Find a docs entry by id. Same schema-level MIME
    enforcement as :func:`resolve_sheet`."""
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
- `NoCacheAvailable` (L154 fix):
  - With `path="/some/dir/foo.json"` → message ends in
    `missing at /some/dir/foo.json`; `exc.path` is the
    string.
  - With `path=None` (the resolver call path) → message ends
    in `not provided`; `exc.path is None`.
  - Both call shapes still surface `exc.kind`.
- `NoCacheAndNetworkDown` carries both `kind` and
  `network_error` attributes.
- `ChannelAmbiguous` carries `name` + `candidate_ids` (list of
  string) attributes; message mentions each candidate id.
- All five subclasses inherit from `RegistryCacheError`, which
  inherits from `Exception`.

### 5.2 `test_registry_cache_schemas.py`

- Each cache model rejects unknown fields (`extra="forbid"`).
- Each cache model enforces its `Literal[<kind>]`
  discriminator (slack_channels / google_sheets_items /
  google_docs_items). Passing the wrong kind → `ValidationError`.
- `fetched_at` accepts only UTC datetimes via the shared
  `_require_utc` validator (reviewer L223 + L229 fix).
  Three sub-pins per cache model so a future refactor that
  drops the validator from one model is a surfaced test
  failure:
  1. Naive datetime → `ValidationError` (no tzinfo).
  2. Non-UTC offset (e.g. `timezone(timedelta(hours=5))`) →
     `ValidationError` mentioning utcoffset (L229
     UTC-only enforcement). Pin all three cache models.
  3. UTC datetime (tz-aware, `utcoffset() == timedelta(0)`)
     → accepted; round-trips through `model_dump_json` /
     `model_validate_json` preserves the value exactly.
- `GoogleSheetsEntry.mime_type` only accepts the Literal
  spreadsheet MIME; assigning a docs MIME →
  `ValidationError` (reviewer L398 fix). Same for
  `GoogleDocsEntry` with the docs MIME literal.
- Round-trip through `model_dump_json` + `model_validate_json`
  preserves every field on every model.
- Empty `channels` / `items` / `docs` list is valid (a freshly
  emptied workspace is legal).
- `owner_id` property returns `workspace_id` on
  `SlackChannelsCache`, `account_id` on the two Google
  caches (reviewer L295 rename).

### 5.3 `test_registry_cache_paths.py`

- `cache_path(kind)` returns the documented filename under
  `DEFAULT_CACHE_BASE`.
- `cache_path(kind, base=tmp_path)` returns the tmp-prefixed path.
- Unknown `kind` value raises `KeyError`.

### 5.4 `test_registry_cache_loader.py`

Load happy path:

- Save then load round-trips a `SlackChannelsCache` snapshot
  unchanged (including `etag=None`).
- Save then load round-trips `GoogleSheetsCache` /
  `GoogleDocsCache`.

Missing file:

- `load_cache(kind, base=tmp_path)` with no file on disk
  returns `None` (not exception).

Owner-id mismatch (per L295 rename + L397 log assertion):

- Save with `workspace_id="T_OLD"`, load with
  `expected_owner_id="T_NEW"` → returns `None`. Pin: the file
  is NOT deleted by the loader (manual review path per Q7).
- Same shape against `GoogleSheetsCache` /
  `GoogleDocsCache` keyed by `account_id`.
- L397 yellow fix: `caplog.records` (or `caplog.text`)
  contains a single WARNING-level record naming the kind,
  expected owner_id, and found owner_id. Pin via
  `caplog.set_level(logging.WARNING, logger="app.v2.registry_cache.loader")`
  so a future change that silently drops the log line
  surfaces. The matching-owner-id load path produces NO
  warning record (inverse pin so a future change that
  accidentally logs on every load surfaces too).

Kind-mismatch save guard (per L314 fix):

- `save_cache("slack_channels", docs_snapshot, base=tmp_path)`
  → `ValueError` raised BEFORE any file I/O. Pin: no file
  is written to `tmp_path` (assert directory empty after).
- Inverse: `save_cache("slack_channels", slack_snapshot)`
  with matching kinds writes successfully.

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

Freshness helper (per Q5):

- `is_stale(cache, now=t0, ttl=24h)` returns False when
  `cache.fetched_at == t0`.
- Returns False at the boundary (`now - fetched_at == ttl`).
- Returns True when `now - fetched_at > ttl`.
- Pure function — does NOT import any clock. Pin by
  asserting `inspect.getsource(is_stale)` contains no
  `datetime.now` AST node (mirrors the phase-5 AST-style
  pin in `test_runtime_defaults.py`).

### 5.5 `test_registry_cache_refresh.py`

Clock parameter required (per L347 fix):

- `inspect.signature(refresh_slack_channels).parameters["clock"].default`
  is `inspect.Parameter.empty`. Same pin for
  `refresh_google_sheets` and `refresh_google_docs`.
- The module does NOT import
  `app.v2.runtime._defaults`. Pin via
  `sys.modules` snapshot before + after importing `refresh`.

Slack:

- A stub client returning two channels produces a
  `SlackChannelsCache` with two `SlackChannelEntry` rows in
  order; `fetched_at` matches the injected `clock`.
- Stub client returning an empty iterable → cache with empty
  `channels` list.
- `expected_owner_id` is recorded as the snapshot's
  `workspace_id` (caller-supplied; client iteration does not
  override it). Pin so a future refactor that resolves
  workspace_id from the client payload is a deliberate change.
- The module does NOT import `slack_sdk` at module load:
  pin via `sys.modules` snapshot before + after importing
  `refresh`.

Sheets / Docs (per L398 + Q3):

- Stub Drive client returning a sheets-MIME items list
  produces a `GoogleSheetsCache` shape with each
  `GoogleSheetsEntry.mime_type` equal to the spreadsheet
  literal.
- `refresh_google_sheets` filters by
  `mimeType='application/vnd.google-apps.spreadsheet'`
  inside the call (assert via spy on the client's
  `list_files(query=...)` parameter).
- `refresh_google_docs` filters by the docs MIME
  (analogous spy assertion).
- If the stub client returns a row whose `mimeType` does NOT
  match the requested kind, the refresh call surfaces a
  pydantic `ValidationError` from the entry constructor —
  defence-in-depth against a buggy client filter.
- The module does NOT import `googleapiclient` at module
  load (same `sys.modules` snapshot pattern).

Client errors propagate:

- Client iterable raising `RuntimeError("network down")` →
  `refresh_*` propagates the original exception unchanged.
  Phase 6 does NOT wrap into `NoCacheAndNetworkDown`; the
  authoring layer (phase 7) is the call site that catches +
  composes (per Q8 confirmation).

### 5.6 `test_registry_cache_resolver.py`

Channels:

- `resolve_channel("C012", cache)` returns the entry by id.
- `resolve_channel("#general", cache)` returns the entry by
  name (strips leading `#`).
- `resolve_channel("general", cache)` returns the entry by
  name (no `#`).
- `resolve_channel("missing", cache)` → `CacheMiss`.
- `resolve_channel("C012", cache=None)` → `NoCacheAvailable`
  with `exc.path is None` (per L154 fix). Message ends in
  `not provided`. Same shape for `resolve_sheet` /
  `resolve_doc` `cache=None` paths.
- Archived channels excluded by default (per Q6): cache
  contains one archived + one active entry sharing a name;
  default call returns the active one. Same call with
  `include_archived=True` returns whichever the search hits
  first (id-keyed lookup remains deterministic).
- Duplicate active name → `ChannelAmbiguous` (per L714 fix +
  Q6). Cache contains two active channels both named
  `"alerts"` with distinct ids. `resolve_channel("alerts",
  cache)` raises; `exc.candidate_ids` lists both ids in
  cache order. ID-keyed lookup still works
  (`resolve_channel("C001", cache)` returns the entry).
- Archived duplicate names do NOT trigger
  `ChannelAmbiguous`: the archived entry is filtered before
  the duplicate check.

Sheets / Docs (per L398):

- `resolve_sheet(id, cache)` returns the `GoogleSheetsEntry`.
- `resolve_sheet("nope", cache)` → `CacheMiss`.
- `resolve_sheet(..., cache=None)` → `NoCacheAvailable`.
- `resolve_sheet(id, cache)` type-checks against
  `GoogleSheetsCache`; passing a `GoogleDocsCache` is a
  static type error AND a runtime AttributeError (no
  `items` attribute on a docs cache).
- `resolve_doc` mirrors `resolve_sheet` against the docs
  cache, including the inverse type-mismatch check.

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
2. `errors.py` ships FIVE `RegistryCacheError` subclasses
   (`NoCacheAvailable`, `CacheMiss`, `WorkspaceMismatch`,
   `NoCacheAndNetworkDown`, `ChannelAmbiguous`); smoke tests
   pin the inheritance + init signature.
3. `schemas.py` ships three cache models
   (`SlackChannelsCache`, `GoogleSheetsCache`,
   `GoogleDocsCache`) with `extra="forbid"`, shared
   UTC-only `fetched_at` validator (round-2 reviewer L904
   wording fix — the invariant is utcoffset == 0, not just
   tz-aware), MIME-literal entry classes, and a uniform
   `owner_id` property.
4. `paths.py` resolves filenames `slack_channels.json` /
   `google_sheets_items.json` / `google_docs_items.json`
   under `DEFAULT_CACHE_BASE = data/cache/registry/`.
5. `loader.py` round-trips every kind, returns `None` for
   missing file, returns `None` for owner-id mismatch
   without deleting the file, raises `RegistryCacheError` on
   corrupt JSON / schema-invalid payload, writes atomically
   via tmp + rename, and `save_cache` raises `ValueError`
   BEFORE any I/O when the snapshot's kind discriminator
   does not match the requested kind (L314).
6. `is_stale(cache, *, now, ttl=timedelta(hours=24))` is a
   pure function that does NOT import any clock. Pinned via
   AST inspection of its body (Q5).
7. `refresh.py` exposes one function per kind
   (`refresh_slack_channels`, `refresh_google_sheets`,
   `refresh_google_docs`), each taking a Protocol-typed
   client + REQUIRED `clock` keyword (no default — L347).
   The module does NOT import `slack_sdk` /
   `googleapiclient` / `app.v2.runtime._defaults` at module
   load. Client errors propagate unchanged (no
   `NoCacheAndNetworkDown` wrap inside `refresh.py`).
8. `resolver.py` resolves channels by id or name (with the
   `#` prefix stripped), filters archived channels by
   default, raises `ChannelAmbiguous` on duplicate active
   names (L714, Q6), raises `NoCacheAvailable` for
   `cache=None`, raises `CacheMiss` for valid cache +
   missing lookup. Sheets / docs resolvers bind to
   kind-specific caches (L398); a docs payload cannot pass
   as a sheet.
9. `app/v2/models/common.py` docstrings no longer reference
   "phase-3" for the registry; the new docstring cites
   phase 6 + `app/v2/registry_cache/resolver.py`.
10. `__init__.py:__all__` covers every public symbol; smoke
    import test passes.
11. Phase guard clean against `v2-phase-5-complete`. No v1
    paths touched. `run_bot.py` untouched.
12. `boot_runtime` untouched (phase 6 ships no boot
    integration). The resolver does NOT load from disk;
    callers (phase 7 authoring layer) call `load_cache` and
    pass the result to `resolve_*`. The "lazy" part of
    §5.7's boot story lives at the phase-7 call site.
13. Full v2 test suite passes (existing 1205 + phase-6 adds);
    no regressions.
14. Annotated git tag `v2-phase-6-complete` created and
    pushed (gated on explicit Sergey approval per workflow
    pattern Sergey pre-approved at phase 6 kickoff).

---

## 8. Tag annotation

```
v2 phase 6 complete

Registry cache for Slack channels / Google Sheets items /
Google Docs items. Pure storage + lookup layer with
provenance (workspace_id / account_id, fetched_at, source,
etag), owner-id mismatch invalidation, atomic on-disk writes,
MIME-literal entry classes for the Google kinds (defence in
depth against cross-type payload), and DI'd refresh
primitives that take a REQUIRED clock parameter and do NOT
import slack_sdk / googleapiclient / runtime._defaults at
module load.

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

### 9.1 Closed in this revision (round-1 reviewer)

1. ~~Cache base directory.~~ **CLOSED.** Design §5.7 example
   uses `data/cache/...`; phase 6 nests under
   `data/cache/registry/` to leave room for future cache
   subsystems without clashing.
2. ~~Boot-time eager fetch.~~ **CLOSED.** §5.7 explicitly says
   "Boot does NOT block on Slack/Drive reachability"; phase 6
   keeps `boot_runtime` untouched and uses lazy load.
3. ~~Drive items + Docs split vs unified.~~ **CLOSED** (Q3
   answer): split into Sheets + Docs (NOT Drive + Docs).
   Filenames are `google_sheets_items.json` and
   `google_docs_items.json`; the corresponding cache classes
   carry MIME-literal entry constraints so the resolver
   cannot accept cross-type payload (also closes L398).
4. ~~`etag` semantics.~~ **CLOSED** (Q4 answer): keep native
   etag optional. Slack stays `None`; refresh does NOT
   compute a content hash to back-fill the field.
5. ~~Stale-warning TTL.~~ **CLOSED** (Q5 answer): phase 6
   ships a pure `is_stale(cache, *, now, ttl=24h)` helper.
   Caller decides whether to log / refresh; helper imports
   no clock.
6. ~~`name` collisions for channels.~~ **CLOSED** (Q6
   answer): raise `ChannelAmbiguous` on duplicate ACTIVE
   names; archived ignored by default
   (`include_archived=False`). IDs stay canonical.
7. ~~Workspace-mismatch policy.~~ **CLOSED** (Q7 answer):
   leave the mismatched file in place; no rename
   complexity. Loader returns `None`.
8. ~~Authoring-layer composite (`NoCacheAndNetworkDown`).~~
   **CLOSED** (Q8 answer): phase 6 ships the class only;
   phase 7 owns the raise path.
9. ~~Tz-aware `fetched_at` enforcement.~~ **CLOSED** (L223 +
   L229 fix): shared `_require_utc` field validator on every
   cache model. UTC-only (utcoffset == 0) — not just
   tz-aware. Three-pin test per model (naive / non-UTC /
   UTC accepted).
10. ~~`save_cache` kind guard.~~ **CLOSED** (L314 fix):
    `save_cache` raises `ValueError` BEFORE any I/O when
    `snapshot.kind != kind`.
11. ~~`expected_workspace_id` covers Google account_id.~~
    **CLOSED** (L295 rename): unified `expected_owner_id`
    parameter across loader + refresh. Cache classes expose
    matching `owner_id` property.
12. ~~`clock=prod_clock` couples cache to runtime.~~
    **CLOSED** (L347 fix): `clock` is a REQUIRED
    keyword-only parameter on every refresh function;
    package does NOT import `app.v2.runtime._defaults`.

### 9.1.a Closed in round-2 reviewer

13. ~~UTC-only vs tz-aware ambiguity.~~ **CLOSED** (L229
    fix): `_require_utc` enforces `utcoffset() ==
    timedelta(0)`; non-UTC offsets raise. Validator renamed
    from `_require_tz_aware` so the contract is obvious at
    call sites.
14. ~~`NoCacheAvailable(kind, path)` requires path the
    resolver doesn't have.~~ **CLOSED** (L154 fix): `path`
    is now optional with `default=None`. Loader callers pass
    the on-disk path; resolver callers pass `None`. Message
    body switches between `missing at <path>` and `not
    provided` so logs are unambiguous.
15. ~~Owner-mismatch log emission unpinned.~~ **CLOSED**
    (L397 fix): §5.4 now asserts a single WARNING-level
    `caplog` record on mismatch, no record on matching
    owner.
16. ~~Stale subclass count in §1.6.~~ **CLOSED** (L71 nit):
    "five subclasses" reflecting `ChannelAmbiguous`.

### 9.1.b Closed in round-3 reviewer (slice 3)

17. ~~Slack archive policy at refresh time.~~ **CLOSED**
    (slice-3 round-3 fix): the cache mirrors the FULL
    ``conversations.list`` result — archived + active —
    verbatim. The resolver filters by ``is_archived`` at
    lookup time via ``include_archived`` (default ``False``).
    ``SlackChannelsClient.list_conversations`` accepts no
    archive-filter kwarg; any such filter belongs to the
    production wrapper or the resolver, not the cache layer.
    Tests pin (a) the cache shape carries an archived entry
    and (b) the Protocol signature has no
    ``exclude_archived`` parameter.

### 9.2 Still open

None — round-3 reviewer closed the slice-3 archive policy.
New items will populate here if reviewer rounds 4+ surface
gaps.

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
