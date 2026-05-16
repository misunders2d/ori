# Scheduler v2 — Phase 10 plan

Phase 9 shipped 2026-05-16 (tag `v2-phase-9-complete` on
origin at `1960305`). Phase 10 is **§12 step 10: source
loaders + snapshot infrastructure** — the four phase-1
concrete source loaders (`source_literal`,
`source_local_file`, `source_slack_thread`,
`source_drive_file`) plus the per-fire snapshot writer and
the per-source cache / fallback / live-change / retention /
on_oversize policy layer.

Phase 10 is a **build-the-layer phase, not a cutover**.
Same cadence as phases 6–8: the loader + snapshot +
resolver machinery is constructed, unit-pinned, and
registered into the `SOURCES` registry — but it is NOT
wired into the worker fire path. The worker emit branch
still rejects `execution_plan_hash`-bearing specs with
`UnsupportedSpecError` (worker.py:454). Source-driven
schedules only start firing at **step 11 (source
templates: `RecurringSeriesFromSource`, `ChannelDigest`)**,
which depends on both step 10 (this phase) and step 9
(shipped). No `CoordinatorAgent`, `run_bot.py`, or
`app/agent.py` edits in phase 10.

The v1-path-forbidden gate is already lifted (phase ≥ 9 →
`is_forbidden` returns False, `check_phase_scope.py:262`).
Phase 10 does not touch any v1 path regardless.

Read this with:
- `docs/CONTRACTS_V2_DESIGN.md` §5.3 (source-driven
  content — the loader return contract, §5.3.1 local-file
  security fence, §5.3.2 per-source cache + fallback,
  §5.3.3 live-source change policy, §5.3.4 item-id
  strategy, §5.3.5 per-fire snapshot + retention, §5.3.6
  sources READ-ONLY, §5.3.7 verbatim preservation), §10.2
  (recurring series with source + state — the consuming
  use case), §12 step 10 + dependency graph (step 11
  depends on 10 + 9), §12.1 invariant 2 (no production
  side effects before the consuming step).
- `docs/PHASE_9_PLAN.md` §10 (slice ledger format), §12.1
  (commit discipline).

---

## 1. Scope statement

### In scope (phase 10)

1. **Four concrete source loaders**, each conforming to
   the §5.3 shared return contract
   (`{content, metadata:{source_kind, source_id,
   fetched_at, content_hash, item_count, source_version,
   selection_method}}`), each registered into `SOURCES`
   (`app/v2/registry.py:243`) via a `SourceDescriptor`
   (`app/v2/descriptors/source.py:46`) with READ_EXTERNAL
   / FILESYSTEM_READ tags only:
   - `source_literal` — frozen user/prompt bytes; zero
     I/O; `selection_method = content_hash`.
   - `source_local_file` — text/markdown/json/yaml under
     an admin-allowlisted root; the full §5.3.1 security
     fence.
   - `source_slack_thread` — read-only thread message
     fetch via the existing Slack protocol DI (no vendor
     SDK at module load; mirrors phase-9 emit layer).
   - `source_drive_file` — Google Docs / Sheets via the
     Drive API; OAuth via the existing registry-cache
     credential path; `USES_OAUTH` tag.
2. **Per-fire snapshot writer**: content → on-disk
   `data/contract_audit/<schedule_id>/<run_id>/sources/<source_id>.json`
   + a `source_snapshots` row via the existing
   `insert_snapshot` (`app/v2/storage/source_snapshots.py:77`).
   Content-addressed (`sha256:`); dedup-by-content-hash.
3. **Retention + on_oversize policy** (per ScheduleSpec
   per §5.3.5): `keep_last_n_snapshots` (default 30),
   `dedup_by_content_hash` (default true), `redact_fields`,
   `max_snapshot_bytes` (default 1_000_000), `on_oversize ∈
   {fail_and_alert, store_pointer_only, redact_and_store,
   hash_only_no_replay}` (enum already exists:
   `OnOversizePolicy`, `enums.py:218`).
4. **Per-source cache + fallback** (per source per
   §5.3.2): `cache_ttl_seconds`, `stale_max_age_seconds`,
   `fallback_policy` (`SourceFallbackPolicy`,
   `enums.py:256`). Mirrors the
   `app/v2/registry_cache/loader.py` load/save/is_stale
   shape. Auth failure NEVER triggers cache fallback —
   routes to failure for re-auth (§5.3.2).
5. **Live-source change policy** (`LiveChangePolicy`,
   `enums.py:239`): `allow` / `alert_on_shape_change` /
   `require_reapprove_on_shape_change`; shape = item_count
   or schema delta vs last snapshot.
6. **Item-id / selection-method strategy** (§5.3.4):
   sheets/docs → stable row id / heading anchor /
   revision-pinned id; plain text/markdown → normalized
   content hash; selection method recorded per fire in the
   `source_resolved` event payload.
7. **Resolver orchestrator** (`resolve_source(...)`):
   dispatch loader via `SOURCES.require(loader)` → apply
   per-source cache → on miss/stale apply fallback_policy
   → snapshot write (retention + on_oversize) → live-change
   shape check → append one of `SOURCE_RESOLVED` /
   `SOURCE_DRIFT_DETECTED` / `SOURCE_FAILED`
   (`EventKind`, `enums.py:128-130`, already present;
   events-table CHECK already allows them). Standalone +
   unit-pinned. NOT called from the worker in phase 10.
8. **Policy model placement** (see §9 Q1 — design
   question): the per-source policy fields
   (cache/fallback/live-change) and the per-ScheduleSpec
   retention block need a typed home. Plan proposes a new
   `SourceRefSpec` model; ratification gated on codex
   round-1.

### Out of scope (phase 10)

- **Worker fire-path integration.** The worker continues
  to reject `execution_plan_hash` specs. Source resolution
  wires into the fire path at step 11 (source templates).
- **Source templates** (`RecurringSeriesFromSource`,
  `ChannelDigest`) — step 11.
- **`source_google_keep`** — phase 2+ (separate
  `keep.googleapis.com` API; awaits Keep auth flow per
  §5.3 note).
- **Read-only-reasoning runtime enforcement** — step 12.
- **Cross-fire state / locks / CAS** — step 13.
- **Compatibility worker, v1→v2 migration, Telegram emit,
  snoozable reminders** — later phases / migration track.
- Any `CoordinatorAgent` / `run_bot.py` / `app/agent.py`
  edit. Phase 10 adds no agent-facing tools (loaders are
  resolver-invoked, not LLM-invoked).

---

## 2. New file paths

```
app/v2/sources/__init__.py            # package + register-on-import
app/v2/sources/literal.py             # source_literal loader
app/v2/sources/local_file.py          # source_local_file + §5.3.1 fence
app/v2/sources/slack_thread.py        # source_slack_thread loader
app/v2/sources/drive_file.py          # source_drive_file loader
app/v2/sources/contract.py            # shared SourceLoader Protocol + return-shape model
app/v2/sources/resolver.py            # resolve_source orchestrator
app/v2/sources/snapshot_writer.py     # per-fire snapshot write + retention + on_oversize
app/v2/sources/cache.py               # per-source cache + fallback
app/v2/models/source_ref.py           # SourceRefSpec (pending §9 Q1)

tests/v2/test_source_literal.py
tests/v2/test_source_local_file.py    # fence: traversal / symlink / deny-list / mime / size
tests/v2/test_source_slack_thread.py
tests/v2/test_source_drive_file.py
tests/v2/test_source_contract.py      # return-shape conformance across all 4
tests/v2/test_source_resolver.py      # orchestration + event emission
tests/v2/test_source_snapshot_writer.py
tests/v2/test_source_cache.py
tests/v2/test_models_source_ref.py
tests/v2/test_phase10_import_hygiene.py  # AST pin (mirrors phase-9)
```

Carried-forward edits: `app/v2/enums.py` only if a new
enum surfaces (none expected — all five source enums
already exist). `docs/CONTRACTS_V2_DESIGN.md` only if a
§5.3 amendment is ratified (see §9).

---

## 3. Module APIs (sketch — finalised per slice)

### 3.1 `SourceLoader` protocol (`sources/contract.py`)

```python
class SourceResult(BaseModel):
    content: JsonValue            # opaque; bytes-faithful (§5.3.7)
    source_kind: str
    source_id: str
    fetched_at: datetime          # tz-aware UTC
    content_hash: str             # sha256:<64hex>
    item_count: int
    source_version: Optional[str]
    selection_method: SelectionMethod

class SourceLoader(Protocol):
    descriptor: SourceDescriptor
    async def load(self, *, args: dict, as_of_datetime:
        Optional[datetime], clock: Callable[[], datetime],
        ...DI...) -> SourceResult: ...
```

All loaders take injected `clock` + transport DI (no
`datetime.now` / `uuid.uuid4`; no vendor SDK at module
load — phase-9 hard rule carries forward).

### 3.2 `source_local_file` fence (§5.3.1) — explicit

`allowed_roots`: admin-config allowlist, empty default.
Deny-list always wins: `data/vault*`, `.env*`, secrets,
`data/contract_state/`, `data/ori-scheduler.db`,
`data/contract_audit/`, `data/contracts/`. Path =
`os.path.realpath(os.path.abspath(p))`, must start with an
allowlisted root prefix (rejects `..` + symlink escape).
`max_bytes` default 1 MiB → routes to `on_oversize`. Mime
allowlist: text/markdown/json/yaml; binary refused unless
explicit opt-in.

### 3.3 `resolve_source` orchestrator (`sources/resolver.py`)

```python
async def resolve_source(
    ref: SourceRefSpec, *, run_id: str, schedule_id: str,
    as_of_datetime: Optional[datetime], conn_factory,
    clock, event_id_factory, ...DI...,
) -> SourceResolution   # ok | drift | failed (typed)
```

Steps: cache check → loader dispatch (`SOURCES.require`)
→ fallback on miss/stale (auth-fail bypasses cache) →
snapshot write (retention + on_oversize) → shape/drift
check → append exactly one EventLedger event. Never
raises into the caller for an expected failure — returns
a typed resolution (mirrors phase-9 `SlackPostResult`).

### 3.4 Snapshot writer (`sources/snapshot_writer.py`)

Dedup: if `dedup_by_content_hash` and a row with the same
`content_hash` exists (`list_snapshots_by_hash`), reuse
its `content_path`, write only the new
`source_snapshots` row. Retention prune keeps last N per
`(schedule_id, source_id)`. `on_oversize` branches are
explicit, no silent fallback (§5.3.5).

---

## 4. Slice ordering + commit cadence

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan) | `docs/PHASE_10_PLAN.md` + `.v2-current-phase` 9→10 + `PHASE_ALLOWLIST[10]` + (cond.) §5.3 design amendment | — |
| 1 | `sources/contract.py` (`SourceResult` + `SourceLoader` Protocol) + `models/source_ref.py` (`SourceRefSpec`, pending Q1) | `test_source_contract.py`, `test_models_source_ref.py` |
| 2 | `source_literal` + register | `test_source_literal.py` |
| 3 | `source_local_file` + full §5.3.1 fence | `test_source_local_file.py` (traversal/symlink/denylist/mime/size matrix) |
| 4 | `snapshot_writer.py` (write + dedup + retention + on_oversize) | `test_source_snapshot_writer.py` |
| 5 | `cache.py` (per-source cache + fallback; auth-fail bypass) | `test_source_cache.py` |
| 6 | `source_slack_thread` (Slack protocol DI) | `test_source_slack_thread.py` |
| 7 | `source_drive_file` (Drive API + OAuth via registry-cache cred path) | `test_source_drive_file.py` |
| 8 | `resolver.py` (orchestration + event emission + live-change) + `test_phase10_import_hygiene.py` AST pin | `test_source_resolver.py`, `test_phase10_import_hygiene.py` |
| closeout | §7 acceptance walk + tag `v2-phase-10-complete` (gated on codex pass) | — |

~9 slices. Reviewer may bundle 2+3 (literal+local-file) or
6+7 (slack+drive). Each slice: implement → commit (no
push) → pause for codex verdict → fix-on-HOLD → GO next.

---

## 5. Test inventory (highlights)

- **`test_source_contract.py`** — all 4 loaders return a
  `SourceResult` that round-trips; `content_hash` is
  `sha256:` of the verbatim bytes; `selection_method` ∈
  the descriptor's `supported_selection_methods`.
- **`test_source_local_file.py`** — the fence matrix:
  `..` traversal refused; symlink escaping an allowed
  root refused (realpath); deny-list paths refused even
  if under an allowlisted root; non-allowlisted mime
  refused; oversize routes to each `on_oversize` branch;
  empty-allowlist default refuses everything.
- **`test_source_snapshot_writer.py`** — on-disk path
  shape; `source_snapshots` row via `insert_snapshot`;
  dedup reuses `content_path`; retention prunes to
  `keep_last_n`; each `on_oversize` branch pinned; redact
  fields stripped before write.
- **`test_source_cache.py`** — fresh hit; stale →
  fallback per policy; `use_last_good_snapshot` reads the
  last `source_snapshots` row; auth-failure NEVER caches
  (routes to failed); `alert_and_use_default` only when a
  user-explicit default is present.
- **`test_source_resolver.py`** — happy → `SOURCE_RESOLVED`
  event + snapshot row; shape change under
  `alert_on_shape_change` → `SOURCE_DRIFT_DETECTED` +
  still resolves; under `require_reapprove_on_shape_change`
  → `SOURCE_FAILED`, no snapshot; loader exception →
  `SOURCE_FAILED`; exactly one event per call; resolver
  never raises for expected failures.
- **`test_phase10_import_hygiene.py`** — every phase-10
  NEW module: no module-load `app.v2.runtime._defaults`
  import, no `uuid.uuid4` / `datetime.now` call, no
  vendor SDK (`slack_sdk` / `googleapiclient` / `httpx`)
  at module load (transport via DI). Mirrors
  `test_phase9_import_hygiene.py`.
- Carry-forward: `test_models_execution_plan.py` /
  `test_descriptors_source.py` updated only if Q1
  changes `InputSpec` or `SourceDescriptor`.

---

## 6. CI guard checks

`PHASE_ALLOWLIST[10]` (slice 0):

```python
PHASE_ALLOWLIST[10] = {
    "app/v2/",
    "tests/v2/",
    "scripts/check_phase_scope.py",
    "scripts/install_hooks.py",
    ".githooks/v2_phase_guard.sh",
    ".githooks/pre-commit",
    ".github/workflows/v2_phase_guard.yml",
    ".v2-current-phase",
    "docs/PHASE_10_PLAN.md",
    "docs/CONTRACTS_V2_DESIGN.md",
    "docs/INDEX.md",
    "docs/AGENTS_INVENTORY.md",
    ".docs_read_marker",
}
```

NOT carried from phase 9: `app/sub_agents/coordinator_agent.py`,
`run_bot.py`, `app/agent.py` — phase-9 cutover-only, no
phase-10 need (build-the-layer phase). `FORBIDDEN_PHASES_PRE_CUTOVER`
unchanged (already inert at phase ≥ 9).

Cross-cutting smoke (carry-forward): phase-10 NEW modules
import no I/O libs at module load (Slack/Drive via
protocol-typed DI per phase-6/9 carry-forward); no
module-load `_defaults` import; clock + id factories stay
DI.

---

## 7. Acceptance criteria for `v2-phase-10-complete`

1. Branch ahead of `v2-phase-9-complete` by N small
   per-slice commits.
2. All 4 loaders registered in `SOURCES`; each descriptor
   READ-ONLY-tag-valid; return-shape conformance pinned.
3. `source_local_file` fence: traversal / symlink /
   deny-list / mime / size all refused per the §5.3.1
   matrix; empty-allowlist default denies all.
4. Snapshot writer: on-disk + `source_snapshots` row;
   dedup-by-hash; retention prune; every `on_oversize`
   branch pinned; redact applied pre-write.
5. Per-source cache + fallback pinned; auth-failure
   bypasses cache; `alert_and_use_default` gated on
   user-explicit default.
6. Live-change policy: `allow` / `alert_on_shape_change`
   / `require_reapprove_on_shape_change` each pinned with
   the matching EventLedger event.
7. `resolve_source` emits exactly one of `SOURCE_RESOLVED`
   / `SOURCE_DRIFT_DETECTED` / `SOURCE_FAILED` per call;
   never raises for an expected failure.
8. Worker fire path UNCHANGED — still rejects
   `execution_plan_hash`; resolver not called from the
   worker (step-11 boundary held).
9. No `datetime.now` / `uuid.uuid4` / vendor-SDK
   module-load outside the sanctioned sites; AST pin on
   every phase-10 NEW module.
10. v1 paths untouched; no `coordinator_agent.py` /
    `run_bot.py` / `app/agent.py` edit.
11. Phase guard `--diff v2-phase-9-complete` + `--staged`
    exit 0.
12. Full v2 test suite green (1888 baseline + phase-10
    adds); no regressions.
13. Annotated tag `v2-phase-10-complete` created (push
    gated on codex CLOSEOUT pass, per the phase-5..9
    pattern).

---

## 8. Tag annotation (draft — finalised at closeout)

```
v2 phase 10 complete

Source loaders + per-fire snapshot infrastructure. The
four phase-1 concrete loaders (source_literal,
source_local_file, source_slack_thread, source_drive_file)
register into the SOURCES registry behind READ-ONLY
descriptors. Per-fire snapshot writer persists content to
data/contract_audit/<schedule>/<run>/sources/<id>.json +
a source_snapshots row, content-addressed, dedup-by-hash,
with per-ScheduleSpec retention + an explicit on_oversize
branch (no silent fallback). Per-source cache + fallback
mirrors the registry-cache load/stale shape; auth failure
never caches. Live-change policy detects item_count /
schema shape drift and routes allow / alert / re-approve.
resolve_source orchestrates dispatch → cache → snapshot →
drift → exactly one SOURCE_RESOLVED / SOURCE_DRIFT_DETECTED
/ SOURCE_FAILED event.

Build-the-layer phase: NOT wired into the worker fire
path. The worker still rejects execution_plan_hash specs;
source-driven schedules begin firing at step 11 (source
templates). No CoordinatorAgent / run_bot.py edits. v1
scheduler untouched.

NOT shipped (deferred): source templates (step 11),
source_google_keep (phase 2+), read-only-reasoning runtime
enforcement (step 12), cross-fire state (step 13).

Design: docs/CONTRACTS_V2_DESIGN.md §5.3, §12 step 10
Plan:   docs/PHASE_10_PLAN.md
```

---

## 9. Open questions (codex plan review round 1)

**Q1 — per-source policy model placement.** `InputSpec`
(`execution_plan.py:44`) is minimal (`id`, `loader`,
`args`, `cache_for_seconds`, `extra="forbid"`). The
per-source policy fields (`cache_ttl_seconds`,
`stale_max_age_seconds`, `fallback_policy`,
`live_change_policy`) and the per-ScheduleSpec retention
block (`keep_last_n_snapshots`, `dedup_by_content_hash`,
`redact_fields`, `max_snapshot_bytes`, `on_oversize`)
need a typed home. Options:
- (a) extend `InputSpec` with optional policy fields —
  simplest, but widens the ExecutionPlan body-hash
  surface and `extra="forbid"` means a schema bump.
- (b) **new `SourceRefSpec` model** referenced by
  `InputSpec` via an optional `source_ref` field;
  retention as a separate per-ScheduleSpec
  `SnapshotRetentionPolicy` block. **(plan's default
  proposal)** — clean separation, isolates the hash
  impact to specs that actually use a live source.
- (c) policies in a side table keyed by
  `(schedule_id, source_id)` — avoids any model/hash
  change but splits the spec across two stores
  (rejected: violates the "frozen spec is
  self-contained" invariant).
Plan assumes (b). Confirm or redirect.

**Q2 — `cache_for_seconds` vs `cache_ttl_seconds`
overlap.** `InputSpec.cache_for_seconds` already exists
(cross-fire within a wakeup window). §5.3.2's
`cache_ttl_seconds` is the live-source cache TTL. Are
these the same knob (rename/reuse) or two distinct caches
(in-wakeup-window memo vs cross-fire snapshot-backed
cache)? Plan treats them as distinct; flagging for
ratification.

**Q3 — `source_slack_thread` / `source_drive_file`
transport DI.** Phase 9 established protocol-typed
transport DI (no vendor SDK at module load) for the emit
layer. Phase 10 reuses the same pattern for read-side
loaders. Confirm the loaders should take a
`SlackThreadReader` / `DriveFileReader` Protocol injected
by the (future step-11) resolver wiring — same shape as
`SlackProtocol` — rather than importing the registry-cache
Google client directly.

**Q4 — snapshot on-disk root.** §5.3.5 specifies
`data/contract_audit/<schedule_id>/<run_id>/sources/<source_id>.json`.
That root is currently a v1 contract path
(`data/contract_audit/` is in the local-file deny-list,
§5.3.1). Confirm the v2 snapshot writer shares that root
(read by future replay tooling) vs a v2-namespaced root
(e.g. `data/v2_source_snapshots/...`). The
`source_snapshots.content_path` is stored relative, so
either is mechanically fine; this is a
disaster-recovery / RUNBOOK coherence question.

**Q5 — slice count.** 9 slices proposed. Acceptable, or
bundle (literal+local-file) and (slack+drive) into 7?

---

## 10. Hard rules (carried forward from phase 9)

1. Reviewer codex via Sergey relay, slice-gated. Pause
   after each commit; APPROVED → GO next slice.
2. No push mid-phase without explicit approval; closeout
   tag + branch push gated on codex CLOSEOUT PASS
   (auto-push-on-PASS pre-approved per phases 5–9).
3. `app/v2/runtime/_defaults.py` is the SOLE
   `datetime.now` / `uuid.uuid4` binding site;
   `_owner_default.py` the documented env exception. AST
   pin on every phase-10 NEW module
   (`test_phase10_import_hygiene.py`, mirrors phase 9).
4. No vendor SDK (`slack_sdk` / `googleapiclient` /
   `httpx`) at module load in any phase-10 module —
   protocol-typed DI only (phase-6/9 carry-forward).
5. Sources are READ-ONLY (§5.3.6); descriptors forbid
   WRITE_EXTERNAL / SEND_MESSAGE / FILESYSTEM_WRITE
   (already enforced by `SourceDescriptor`).
6. Verbatim byte preservation (§5.3.7) — no LLM
   paraphrase anywhere in the source path.
7. All datetime fields tz-aware UTC; naive raises /
   surfaces as a typed failure.
8. Doc-coupling: behaviour change ships with the matching
   hand-written design/plan doc in the same commit; the
   `ORI_SKIP_DOC_CHECK=1` bypass is harness-denied and
   not used.
9. `.v2-current-phase` reads 10 after slice 0;
   `PHASE_ALLOWLIST[10]` gates scope.
10. Build-the-layer only: NO worker fire-path wiring, NO
    agent-facing tool, NO production side effect before
    step 11 (§12.1 invariant 2).
11. Intentional workspace dirt left alone:
    `app/tools/youtube.py` + `.playwright-mcp/` + the 3
    `scripts/diag_*` / `amazon_ads_mcp_proxy.py`
    untracked.
```
