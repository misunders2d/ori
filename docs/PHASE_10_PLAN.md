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

**Round-2 revision (2026-05-16)** closes 3 bugs + 2 risks
surfaced by codex on the round-1 plan landing (`7e134d7`)
and bakes in the codex Q-call answers:
- 🔴 local-file fence sibling-prefix bypass
  (`startswith` → `Path.is_relative_to`); §3.2 + a pinned
  bypass test.
- 🔴 typed error contract (`SourceAuthError` /
  `SourceFetchError` / `SourceParseError`) so the
  resolver distinguishes auth failure from
  network/parse; auth bypasses cache AND fallback.
- 🔴 snapshot retention semantics defined precisely
  (deletes rows + backing files, content-hash-shared-file
  safe) + a new sanctioned storage mutation + a
  `docs/CONTRACTS_V2_DESIGN.md` §5.3.5 mechanism clause.
- 🟡 canonical-bytes spec per content type (hash + write
  stable across serializers).
- 🟡 Q1: reuse the EXISTING `AuditPolicy` (retention) +
  `LiveSourceCachePolicy` (cache) — NO duplicate
  `SnapshotRetentionPolicy`. `SourceRefSpec` adds only
  the genuinely-missing `live_change_policy` + explicit
  default.

**Round-3 revision (2026-05-16)** closes the 4🔴 + 1🟡
codex surfaced on the round-2 plan landing (`9dcd2a6`) —
largely plan↔design drift (§9a is the full disposition):
- 🔴 `docs/CONTRACTS_V2_DESIGN.md` §5.3.1 rewritten to
  the same `Path.resolve+is_relative_to` fence as plan
  §3.2 (it still said `realpath()+startswith`); design
  doc re-grepped clean for fence/prefix wording.
- 🔴 fence rejects raise NON-fallback
  `SourceSecurityError`; oversize `fail_and_alert` raises
  NON-fallback `SourcePolicyError`. The resolver now has
  a SINGLE rule: `fallback_eligible` ClassVar, True only
  on `SourceFetchError`; everything else →
  `SOURCE_FAILED`, no cache/fallback (a denied path /
  oversize can never serve stale data).
- 🔴 `prune_snapshots` commits row DELETEs BEFORE any
  file unlink (post-commit, best-effort, re-query);
  deleted-file-with-live-row is now structurally
  impossible.
- 🟡 on-disk artifact pinned: `<source_id>.bin` holding
  `content_bytes` verbatim (no JSON envelope);
  `sha256(file)==content_hash`.

**Round-4 revision (2026-05-16)** closes the round-3
codex HOLD — 3🔴, all the recurring plan↔design↔tag
drift class: the plan body was correct but
`docs/CONTRACTS_V2_DESIGN.md` §5.3.5 + the §8
tag-annotation lagged. This pass did a SINGLE full
reconciliation (§9b):
- 🔴 design §5.3.5 on-disk line `…/<source_id>.json` →
  `.bin` raw `content_bytes` verbatim + `sha256(file)==
  content_hash`, verbatim-matching plan §3.6.
- 🔴 design §5.3.5 prune sequence rewritten to COMMIT
  row deletes FIRST then post-commit re-query +
  best-effort unlink on a fresh conn — verbatim-matching
  plan §3.4 (it had reintroduced the in-transaction
  unlink data-loss).
- 🔴 §8 tag-annotation `sources/<id>.json` → `.bin`
  raw `content_bytes` + the corrected prune/error
  wording (ships as the permanent phase record).
Reconciliation grep (`json` / `unlink` / `transaction` /
`<source_id>` / `realpath` / `startswith`) over BOTH
docs: zero substantive divergences remain (residual
`.json` hits are unrelated — registry cache, v1
contracts; residual `realpath`/`startswith` is the
intentional anti-pattern callout + dated disposition
history). plan ≡ design ≡ tag-annotation.

Q-call answers folded in: Q1 `SourceRefSpec` yes (reuse
existing policy models); Q2 distinct cache knobs, both
pinned; Q3 Protocol DI for Slack/Drive readers; Q4 keep
the `data/contract_audit/...` design root (`.bin` file)
+ local-file deny-list still blocks it; Q5 keep 9
slices, do NOT bundle the security-heavy `local_file` /
`cache` slices.

The §5.3.5 retention-mechanism clause is a
`docs/CONTRACTS_V2_DESIGN.md` amendment landed in the
plan-fix commit (rationale in the commit body — it
clarifies, not contradicts, the shipped §5.3.5 retention
intent; the append-only invariant in
`app/v2/storage/source_snapshots.py` is scoped to the
content-addressed INSERT path, with retention pruning a
distinct sanctioned mutation).

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
2. **Per-fire snapshot writer**: writes the §3.6
   `content_bytes` VERBATIM to a raw on-disk file with a
   neutral extension —
   `data/contract_audit/<schedule_id>/<run_id>/sources/<source_id>.bin`
   (🟡 codex round-2 #5 — NOT `.json`: the artifact is
   raw text / yaml / json / binary `content_bytes`, not a
   JSON envelope; the file IS the hashed bytes so
   `sha256(file)` re-verifies `content_hash` trivially;
   metadata lives in the `source_snapshots` row, NEVER in
   the file). Q4 — design root kept; the local-file
   deny-list still blocks reads under it (§3.2). Plus a
   `source_snapshots` row via the existing
   `insert_snapshot` (`app/v2/storage/source_snapshots.py:77`).
   Content-addressed (`content_hash = "sha256:" +
   sha256(content_bytes)`, §3.6); dedup-by-content-hash.
3. **Retention + on_oversize policy** — REUSES the
   EXISTING `AuditPolicy` (`app/v2/models/common.py:194`,
   already a field on `ScheduleSpec.audit`,
   `models/schedule.py:81`): `keep_last_n_snapshots`
   (30), `dedup_by_content_hash` (true), `redact_fields`,
   `max_snapshot_bytes` (1_000_000), `on_oversize`
   (`OnOversizePolicy`, `enums.py:218`). **No new
   `SnapshotRetentionPolicy` model** (codex 🟡 — would
   duplicate `AuditPolicy`). Retention semantics are
   defined precisely in §3.4 (deletes rows + backing
   files, content-hash-shared-file safe) — a NEW
   sanctioned storage mutation, distinct from the
   append-only content-addressed INSERT path.
4. **Per-source cache + fallback** — REUSES the EXISTING
   `LiveSourceCachePolicy` (`app/v2/models/common.py:206`):
   `cache_ttl_seconds`, `stale_max_age_seconds`,
   `fallback_policy` (`SourceFallbackPolicy`,
   `enums.py:256`). Mirrors the
   `app/v2/registry_cache/loader.py` load/save/is_stale
   shape. **Q2 — two distinct cache knobs, both pinned:**
   `InputSpec.cache_for_seconds` (exists,
   `execution_plan.py:67`) = in-wakeup-window memo (same
   loader output reused across fires inside ONE wakeup
   tick); `LiveSourceCachePolicy.cache_ttl_seconds` =
   cross-fire source cache (snapshot-backed, persists
   across wakeup ticks). They are orthogonal: the memo
   short-circuits a repeated fetch within a tick; the TTL
   governs staleness of the persisted last-good snapshot.
   **Auth failure NEVER triggers cache OR fallback** —
   it raises a typed `SourceAuthError` (§3.5) that the
   resolver routes straight to `SOURCE_FAILED` for
   re-auth; only `SourceFetchError` (network) is eligible
   for `fallback_policy` (§5.3.2).
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
8. **`SourceRefSpec` model** (Q1 — RATIFIED by codex
   round-1): a new `app/v2/models/source_ref.py`
   `SourceRefSpec` that COMPOSES the existing policy
   models rather than duplicating them:
   - `loader: str` — registered `SOURCES` key.
   - `args: dict` — loader args.
   - `cache: LiveSourceCachePolicy` — REUSED as-is
     (`common.py:206`).
   - `live_change_policy: LiveChangePolicy` — the
     genuinely-missing field (`LiveChangePolicy` enum
     exists, `enums.py:239`; no model carried it).
   - `explicit_default: Optional[JsonValue]` — required
     iff `cache.fallback_policy ==
     ALERT_AND_USE_DEFAULT` (§5.3.2: valid only when a
     user-explicit default is present AND shown in
     dry-run); a model validator enforces the pairing.
   Retention stays on `ScheduleSpec.audit` (`AuditPolicy`,
   unchanged). `SourceRefSpec` does NOT re-declare
   retention. Whether `InputSpec` gains an optional
   `source_ref: Optional[SourceRefSpec]` field is a
   slice-1 detail (additive, `extra="forbid"` schema
   bump pinned by an `ExecutionPlan` body-hash
   round-trip test) — it does not change any shipped
   hash because absent `source_ref` is stripped from
   `canonical_body` (mirrors the phase-9 `template.args`
   None-strip pattern).

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
app/v2/sources/contract.py            # SourceLoader Protocol + SourceResult + §3.6 canonical-bytes helper
app/v2/sources/errors.py              # SourceError + 5 subclasses; only SourceFetchError is fallback_eligible
app/v2/sources/resolver.py            # resolve_source orchestrator
app/v2/sources/snapshot_writer.py     # per-fire snapshot write + retention prune + on_oversize
app/v2/sources/cache.py               # per-source cache + fallback (auth-fail bypass)
app/v2/models/source_ref.py           # SourceRefSpec (composes LiveSourceCachePolicy + LiveChangePolicy)

tests/v2/test_source_contract.py      # return-shape + §3.6 canonical-bytes hash-stability across serializers
tests/v2/test_source_errors.py        # typed-error taxonomy + isinstance contract
tests/v2/test_source_literal.py
tests/v2/test_source_local_file.py    # fence matrix incl. SIBLING-PREFIX bypass (/safe/root_evil vs /safe/root)
tests/v2/test_source_slack_thread.py
tests/v2/test_source_drive_file.py
tests/v2/test_source_resolver.py      # orchestration + event emission + auth-bypasses-cache-AND-fallback
tests/v2/test_source_snapshot_writer.py  # write + dedup-shared-file safety + retention DELETE rows+files + on_oversize
tests/v2/test_source_cache.py
tests/v2/test_models_source_ref.py    # explicit_default required iff fallback==ALERT_AND_USE_DEFAULT
tests/v2/test_phase10_import_hygiene.py  # AST pin (mirrors phase-9)
```

Edits to existing surface:
- `app/v2/storage/source_snapshots.py` — slice 4 adds the
  NEW sanctioned retention mutation
  `prune_snapshots(conn, *, schedule_id, source_id,
  keep_last_n)` (joins `runs` for `schedule_id` scope —
  `source_snapshots` PK is `(run_id, source_id)`, no
  `schedule_id` column) + amends the module docstring's
  "No update / delete surface" line to scope it to the
  content-addressed INSERT path (retention is the one
  sanctioned delete).
- `app/v2/models/execution_plan.py` — slice 1 adds
  `InputSpec.source_ref: Optional[SourceRefSpec] = None`
  (additive; `canonical_body` strips it when None —
  phase-9 `template.args` pattern; hash round-trip
  pinned).
- `docs/CONTRACTS_V2_DESIGN.md` — slice 0 (this plan-fix)
  adds the §5.3.5 retention-MECHANISM clause (rows+files,
  dedup-safe). `app/v2/enums.py` untouched (all five
  source enums already exist).

---

## 3. Module APIs (sketch — finalised per slice)

### 3.1 `SourceLoader` protocol (`sources/contract.py`)

```python
class SourceResult(BaseModel):
    content: JsonValue            # str | int | float | bool | None | list | dict
    content_bytes: bytes          # the §3.6 CANONICAL encoding actually hashed + written
    source_kind: str
    source_id: str
    fetched_at: datetime          # tz-aware UTC
    content_hash: str             # "sha256:" + sha256(content_bytes).hexdigest()
    item_count: int
    source_version: Optional[str]
    selection_method: SelectionMethod

class SourceLoader(Protocol):
    descriptor: SourceDescriptor
    async def load(self, *, args: dict, as_of_datetime:
        Optional[datetime], clock: Callable[[], datetime],
        ...DI...) -> SourceResult: ...
```

`content_hash` and the on-disk snapshot are computed from
`content_bytes` (the §3.6 canonical encoding), NEVER from
re-serializing `content` — so the hash is stable
regardless of which serializer a reader uses. All loaders
take injected `clock` + transport DI (no `datetime.now` /
`uuid.uuid4`; no vendor SDK at module load — phase-9 hard
rule carries forward). A loader signals failure ONLY via
the §3.5 typed error hierarchy — never a bare
`Exception` / `return None`.

### 3.2 `source_local_file` fence (§5.3.1) — explicit

`allowed_roots`: admin-config allowlist, empty default.
Deny-list always wins: `data/vault*`, `.env*`, secrets,
`data/contract_state/`, `data/ori-scheduler.db`,
`data/contract_audit/`, `data/contracts/`.

**🔴 codex round-1 fix — sibling-prefix bypass.** The
round-1 plan said "must start with an allowlisted root
prefix" → a naive `resolved.startswith(str(root))`
admits `/safe/root_evil/secret.txt` against an allowed
root `/safe/root` (string-prefix, not path-component
match). The fence MUST be **separator-aware**:

```python
resolved = Path(p).resolve(strict=True)        # follows symlinks
root     = Path(allowed_root).resolve(strict=True)
if not resolved.is_relative_to(root):          # py3.9+; component-wise
    raise SourceSecurityError("path_outside_allowed_root")
if _hits_deny_list(resolved):                  # post-resolve()
    raise SourceSecurityError("path_in_deny_list")
```

Every fence rejection raises `SourceSecurityError`
(§3.5 — NON-fallback; a denied read can NEVER serve a
last-good cached snapshot). `Path.resolve(strict=True)`
canonicalises `..` AND resolves every symlink component
before the check, so a symlink escaping the root (the
resolved target falls outside) is rejected too.
`is_relative_to` compares path COMPONENTS, so
`/safe/root_evil` is NOT relative to `/safe/root` (string
prefix would have wrongly passed). The deny-list check
runs on the SAME `resolved` path (post-symlink) so a
symlink INTO `data/vault` is caught. **This mechanism is
identical to `docs/CONTRACTS_V2_DESIGN.md` §5.3.1 verbatim
(plan↔design consistency is load-bearing — codex
round-2 🔴#1); the fence test pins it so a drift in
either doc fails CI.**
`max_bytes` default 1 MiB → routes to `on_oversize`
(§3.4). Mime allowlist: text/markdown/json/yaml; binary
refused unless explicit per-root opt-in. Pinned tests
(§5): the sibling-prefix case, the symlink-escape case,
the symlink-into-denylist case, `..` traversal,
empty-allowlist-denies-all.

### 3.3 `resolve_source` orchestrator (`sources/resolver.py`)

```python
async def resolve_source(
    ref: SourceRefSpec, *, run_id: str, schedule_id: str,
    as_of_datetime: Optional[datetime], conn_factory,
    clock, event_id_factory, ...DI...,
) -> SourceResolution   # ok | drift | failed (typed)
```

Steps: cache check → loader dispatch (`SOURCES.require`)
→ on a raised `SourceError` apply the single §3.5 rule
(`fallback_eligible` ⇔ `SourceFetchError` → consult
`fallback_policy`; ANY other subclass → `SOURCE_FAILED`,
cache + fallback bypassed) → snapshot write (+ on_oversize)
→ shape/drift check → retention prune (post-write) →
append exactly one EventLedger event. Never raises into
the caller for an expected failure — returns a typed
resolution (mirrors phase-9 `SlackPostResult`).

### 3.4 Snapshot writer + retention (`sources/snapshot_writer.py`)

**Dedup:** if `audit.dedup_by_content_hash` and a row
with the same `content_hash` exists
(`list_snapshots_by_hash`), reuse its `content_path` —
write only the new `source_snapshots` row, no second
file.

**🔴 codex round-1 fix — retention semantics (precise).**
Retention DELETES, and the storage layer's current "no
update/delete surface" invariant is scoped to the
content-addressed INSERT path, not retention. Slice 4
adds a NEW sanctioned mutation
`prune_snapshots(conn, *, schedule_id, source_id,
keep_last_n) -> PruneResult`:

1. Select all `source_snapshots` rows for the
   `(schedule_id, source_id)` pair — `schedule_id` via
   `JOIN runs ON runs.id = source_snapshots.run_id`
   (the table has no `schedule_id` column; PK is
   `(run_id, source_id)`).
2. Order by `fetched_at` DESC; keep the newest
   `keep_last_n`; the rest are prune candidates.
3. Collect each prune-candidate's `content_path`, then
   DELETE the candidate rows. **COMMIT the row deletes
   FIRST.**
4. **🔴 codex round-2 fix — unlink AFTER commit, never
   inside the prune transaction.** Files are NOT
   unlinked inside the DB transaction: a later rollback
   would restore rows pointing at already-deleted files
   (= data loss). Order is strict: (a) row DELETEs in
   one `transaction(conn)`; (b) `transaction` COMMITs;
   (c) ONLY THEN, for each collected `content_path`,
   re-query `SELECT 1 FROM source_snapshots WHERE
   content_path = ? LIMIT 1` on a fresh connection — if
   no surviving row references it (content-addressed
   files are shared across dedup'd rows), best-effort
   `unlink`. An orphaned file (row gone, file lingers)
   is harmless and is counted/logged for a separate
   sweep; a deleted file with a live row is data loss
   and this ordering makes it impossible. A unlink
   failure post-commit is logged + counted, never
   re-raised.
5. Return `PruneResult(rows_deleted, files_unlinked,
   files_kept_shared, orphans_logged)` for the
   `source_resolved` / audit event payload.

Retention is invoked by the resolver AFTER a successful
snapshot write, never mid-fetch. `keep_last_n_snapshots
== 0` means "keep none beyond the just-written row"
(prune all older). The `app/v2/storage/source_snapshots.py`
module docstring is amended in the same slice to say:
append-only for `insert_snapshot`; `prune_snapshots` is
the sole sanctioned delete, retention-only, and unlinks
files only AFTER its row-delete transaction commits.

**`on_oversize`** (`content_size > audit.max_snapshot_bytes`)
— explicit branches, no silent fallback (§5.3.5):
- `fail_and_alert` → **`SourcePolicyError`** (🔴 codex
  round-2 #3 — NOT `SourceFetchError`; oversize is a
  policy refusal, NON-fallback, so it can never serve a
  stale cached snapshot) → `SOURCE_FAILED`, no row/file.
- `store_pointer_only` → row written, `content_path`
  empty / pointer sentinel, no file body.
- `redact_and_store` → `audit.redact_fields` stripped,
  re-measured; if still oversize → `fail_and_alert`.
- `hash_only_no_replay` → row + `content_hash` only,
  body discarded (replay impossible, flagged in payload).

### 3.5 Typed error contract (`sources/errors.py`)

**🔴 codex round-1 + round-2 fix.** Fallback eligibility
must be unforgeable. **Exactly ONE subclass is
fallback-eligible (`SourceFetchError`); every other
`SourceError` subclass routes straight to
`SOURCE_FAILED` with cache AND fallback bypassed.** This
single rule (not a per-subclass enumeration) is the
resolver's only branch — so a denied path / oversize /
parse failure can never serve a stale cached snapshot.

```python
class SourceError(Exception):          # base; never raised directly
    code: str                          # machine code for the event payload
    fallback_eligible: ClassVar[bool] = False   # default DENY

class SourceFetchError(SourceError):   # ONLY: transient network —
    fallback_eligible = True           # conn refused / DNS / 5xx /
    # timeout / read reset. The SOLE fallback-eligible class.

class SourceAuthError(SourceError):    # 401/403, token expired, no
    # creds. Non-fallback → SOURCE_FAILED, admin re-auth.

class SourceSecurityError(SourceError):  # 🔴 r2 #2 — path-fence
    # reject, deny-list hit, symlink escape, traversal. A
    # denied read MUST NEVER serve a last-good snapshot.
    # Non-fallback → SOURCE_FAILED.

class SourcePolicyError(SourceError):  # 🔴 r2 #3 — oversize
    # fail_and_alert, mime-not-allowlisted, shape
    # re-approve refusal. Non-fallback → SOURCE_FAILED.

class SourceParseError(SourceError):   # fetched bytes unparseable
    # for the declared kind. Non-fallback → SOURCE_FAILED.
```

**Resolver rule (written, unambiguous):** on any
`SourceError`, if `exc.fallback_eligible` is True
(⇔ `isinstance(exc, SourceFetchError)`) consult
`ref.cache.fallback_policy`; **otherwise** emit
`SOURCE_FAILED(exc.code)` immediately — no cache read,
no fallback, regardless of any warm last-good snapshot.
`fallback_eligible` is a `ClassVar` defaulting to
`False` on the base, set `True` ONLY on
`SourceFetchError`, so a new subclass is non-fallback by
construction (fail-safe default). Loaders raise the
precise subclass; the fence (§3.2) raises
`SourceSecurityError`; oversize `fail_and_alert` (§3.4)
raises `SourcePolicyError`. Pinned tests (§5): with a
warm last-good snapshot present —
`SourceAuthError` / `SourceSecurityError` /
`SourcePolicyError` / `SourceParseError` each STILL emit
`SOURCE_FAILED` and do NOT serve the snapshot; only
`SourceFetchError` + `use_last_good_snapshot` serves it.
A taxonomy test asserts `SourceFetchError.fallback_eligible
is True` and every other subclass `is False`, and that no
non-fetch subclass `isinstance`-leaks into the fetch
branch — so a future loader can't widen the
fallback surface.

### 3.6 Canonical bytes (`sources/contract.py`)

**🟡 codex round-1 fix.** `content_hash` + the on-disk
snapshot must be byte-identical regardless of serializer
(§5.3.7 verbatim). `content_bytes` is computed ONCE at
load time per declared type and is the ONLY thing hashed
/ written:

| declared kind | canonical bytes |
|---|---|
| text / markdown | the raw fetched bytes, **verbatim**, NO normalisation (preserves trailing whitespace / CRLF / BOM) |
| json | `json.dumps(parsed, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")` — canonical form; a re-fetch of semantically-equal JSON yields the SAME hash |
| yaml | the raw fetched bytes verbatim (NOT re-serialized — round-tripping YAML is lossy; treat as opaque text for hash/replay) |
| dict / list (loader-built, e.g. slack_thread) | same canonical-JSON rule as `json` |
| binary (opt-in only) | the raw bytes; `content` carries a `{"_b64": ...}` envelope; hash over the raw bytes |

`content_hash = "sha256:" + sha256(content_bytes).hexdigest()`.

**On-disk artifact (🟡 codex round-2 #5 — pinned).** The
snapshot file (`<source_id>.bin`, §1.2) holds EXACTLY
`content_bytes` and nothing else — no JSON envelope, no
metadata, no base64 wrapper. So `sha256(open(path,
"rb").read())` re-derives `content_hash` with zero
parsing. The in-memory `SourceResult.content` field MAY
carry a `{"_b64": ...}` envelope for binary so the
resolver/state-dict stays JSON-shaped, but that envelope
is NEVER what gets hashed or written — `content_bytes`
(the raw decoded bytes) is. All snapshot metadata
(`source_kind`, `selection_method`, `source_version`,
`fetched_at`, sizes) lives in the `source_snapshots`
row, never in the file.

Pinned test (§5): two loads of semantically-equal JSON
with different key order / whitespace → identical
`content_hash`; a text source with a trailing newline
delta → DIFFERENT hash (verbatim, no normalisation);
the on-disk `.bin` re-hashes to `content_hash` byte-for-
byte for every declared kind incl. binary.

---

## 4. Slice ordering + commit cadence

| Slice | Module(s) | Tests |
|---|---|---|
| 0 (plan-fix) | `docs/PHASE_10_PLAN.md` round-2 + `docs/CONTRACTS_V2_DESIGN.md` §5.3.5 retention-mechanism clause (already staged: `.v2-current-phase` 9→10 + `PHASE_ALLOWLIST[10]` landed round-1 `7e134d7`) | — |
| 1 | `sources/contract.py` (`SourceResult` + `content_bytes` + §3.6 canonical-bytes helper + `SourceLoader` Protocol) + `sources/errors.py` (§3.5 typed hierarchy) + `models/source_ref.py` (`SourceRefSpec` composing `LiveSourceCachePolicy` + `LiveChangePolicy` + `explicit_default` validator) + `InputSpec.source_ref` additive field + `canonical_body` None-strip | `test_source_contract.py` (incl. canonical-bytes hash-stability), `test_source_errors.py`, `test_models_source_ref.py`, `test_models_execution_plan.py` hash round-trip |
| 2 | `source_literal` + register | `test_source_literal.py` |
| 3 | `source_local_file` + full §5.3.1 fence (`Path.is_relative_to`, sibling-prefix + symlink-escape + symlink-into-denylist pinned) | `test_source_local_file.py` |
| 4 | `snapshot_writer.py` (write + dedup-shared-file safety + `on_oversize`) + `storage/source_snapshots.py` `prune_snapshots` retention mutation + docstring amend | `test_source_snapshot_writer.py`, `test_storage_source_snapshots.py` prune cases |
| 5 | `cache.py` (per-source cache + fallback; `SourceAuthError` bypasses cache AND fallback) | `test_source_cache.py` |
| 6 | `source_slack_thread` (Slack-reader Protocol DI) | `test_source_slack_thread.py` |
| 7 | `source_drive_file` (Drive-reader Protocol DI; OAuth via registry-cache cred path) | `test_source_drive_file.py` |
| 8 | `resolver.py` (orchestration + typed-error routing + live-change + exactly-one event) + `test_phase10_import_hygiene.py` AST pin | `test_source_resolver.py`, `test_phase10_import_hygiene.py` |
| closeout | §7 acceptance walk + tag `v2-phase-10-complete` (gated on codex pass) | — |

9 slices (Q5 — kept; the security-heavy `local_file`
(slice 3) and `cache` (slice 5) get separate codex
review, NOT bundled). Each slice: implement → commit (no
push) → pause for codex verdict → fix-on-HOLD → GO next.

---

## 5. Test inventory (highlights)

- **`test_source_contract.py`** — all 4 loaders return a
  `SourceResult`; `content_hash == "sha256:"+sha256(
  content_bytes)`; **§3.6 canonical-bytes stability**:
  two loads of semantically-equal JSON (key-order /
  whitespace differ) → IDENTICAL hash; a text source
  with a trailing-newline delta → DIFFERENT hash
  (verbatim, no normalisation); `selection_method` ∈ the
  descriptor's `supported_selection_methods`.
- **`test_source_errors.py`** — taxonomy (5 subclasses):
  all subclass `SourceError`; **`SourceFetchError.
  fallback_eligible is True` and every other subclass
  (`SourceAuthError` / `SourceSecurityError` /
  `SourcePolicyError` / `SourceParseError`)
  `fallback_eligible is False`**; the base default is
  `False` (fail-safe — a hypothetical new subclass is
  non-fallback by construction); no non-fetch subclass
  `isinstance`-leaks into `SourceFetchError`; each
  carries a machine `code`.
- **`test_source_local_file.py`** — fence matrix:
  **🔴 sibling-prefix bypass** (`/safe/root_evil/x` vs
  allowed `/safe/root` → REFUSED via `is_relative_to`,
  the bug a `startswith` fence would pass); symlink
  whose resolved target escapes the root → refused;
  symlink INTO a deny-list path (`data/vault`) → refused
  on the post-`resolve()` path; `..` traversal refused;
  non-allowlisted mime refused; oversize → each
  `on_oversize` branch; empty-allowlist default refuses
  everything.
- **`test_source_snapshot_writer.py`** — on-disk `.bin`
  file holds `content_bytes` VERBATIM (no envelope) and
  `sha256(file)` == `content_hash` for every declared
  kind incl. binary; `source_snapshots` row via
  `insert_snapshot`; **dedup-shared-file safety**: two
  rows same `content_hash` share one file; pruning ONE
  row does NOT unlink while the other survives; pruning
  the LAST referencing row DOES unlink; **prune ordering
  (🔴 r2 #4)**: row DELETEs COMMIT before any unlink; a
  simulated post-DELETE rollback leaves rows AND files
  intact (no row points at a deleted file — data-loss
  impossible); an orphaned file (row gone, unlink
  failed) is counted in `PruneResult.orphans_logged`,
  not data loss; `prune_snapshots` scoped by
  `schedule_id` via the `runs` join; `keep_last_n==0`
  prunes all older; each `on_oversize` branch pinned
  (`fail_and_alert` → `SourcePolicyError`);
  `redact_fields` stripped + re-measured pre-write.
- **`test_storage_source_snapshots.py`** (extend) —
  `prune_snapshots` happy + dedup-safety + the
  schedule_id-scoping join; the append-only invariant
  still holds for `insert_snapshot`.
- **`test_source_cache.py`** — fresh hit; stale →
  fallback per policy; `use_last_good_snapshot` reads the
  last `source_snapshots` row. **With a warm last-good
  snapshot present, EACH non-fetch subclass
  (`SourceAuthError`, `SourceSecurityError`,
  `SourcePolicyError`, `SourceParseError`) routes to
  `SOURCE_FAILED` and does NOT serve the snapshot**;
  ONLY `SourceFetchError` + `use_last_good_snapshot`
  serves it (proves the single `fallback_eligible` rule
  is load-bearing — a denied path / oversize can never
  serve stale data); `alert_and_use_default` only when a
  user-explicit `explicit_default` is present.
- **`test_models_source_ref.py`** — `SourceRefSpec`
  composes `LiveSourceCachePolicy` + `LiveChangePolicy`;
  validator: `explicit_default` REQUIRED iff
  `cache.fallback_policy == ALERT_AND_USE_DEFAULT`, else
  forbidden; no duplicate retention fields (retention
  lives on `ScheduleSpec.audit`).
- **`test_models_execution_plan.py`** (extend) —
  `InputSpec.source_ref` additive; a spec with
  `source_ref=None` hashes IDENTICALLY to the
  pre-phase-10 body (None stripped from `canonical_body`,
  phase-9 `template.args` pattern); a spec WITH a
  `source_ref` re-hashes deterministically.
- **`test_source_resolver.py`** — happy → `SOURCE_RESOLVED`
  + snapshot row; shape change under
  `alert_on_shape_change` → `SOURCE_DRIFT_DETECTED` +
  still resolves; under `require_reapprove_on_shape_change`
  → `SOURCE_FAILED`, no snapshot; `SourceAuthError` /
  `SourceSecurityError` / `SourcePolicyError` /
  `SourceParseError` each → `SOURCE_FAILED`,
  cache+fallback bypassed (single `fallback_eligible`
  rule); `SourceFetchError` → fallback path; exactly one
  event per call; resolver never raises for expected
  failures.
- **`test_phase10_import_hygiene.py`** — every phase-10
  NEW module: no module-load `app.v2.runtime._defaults`
  import, no `uuid.uuid4` / `datetime.now` call, no
  vendor SDK (`slack_sdk` / `googleapiclient` / `httpx`)
  at module load (transport via DI). Mirrors
  `test_phase9_import_hygiene.py`.

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
3. `source_local_file` fence uses `Path.is_relative_to`
   (separator-aware): the SIBLING-PREFIX bypass
   (`/safe/root_evil` vs `/safe/root`), symlink-escape,
   symlink-into-denylist, `..` traversal, non-allowlist
   mime, oversize all refused; empty-allowlist default
   denies all.
4. Snapshot writer: on-disk `<source_id>.bin` holds
   `content_bytes` verbatim, `sha256(file)==content_hash`
   (every kind incl. binary); `source_snapshots` row;
   dedup-shared-file safety; `prune_snapshots` DELETES
   rows then unlinks files AFTER commit (data-loss
   impossible; orphan ≠ loss), scoped by `schedule_id`
   (runs join); every `on_oversize` branch pinned
   (`fail_and_alert` → `SourcePolicyError`);
   `redact_fields` applied + re-measured pre-write;
   storage docstring amended (insert append-only; prune
   the sole sanctioned delete, unlink post-commit).
5. Typed error contract (5 subclasses): EXACTLY ONE
   (`SourceFetchError`) `fallback_eligible`; each of
   `SourceAuthError` / `SourceSecurityError` /
   `SourcePolicyError` / `SourceParseError` → `SOURCE_FAILED`
   with cache AND fallback bypassed even with a warm
   last-good snapshot (a denied path / oversize NEVER
   serves stale data); base default non-fallback;
   `alert_and_use_default` gated on a present
   `explicit_default`.
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
12. §3.6 canonical bytes: `content_hash` stable across
    serializers (JSON key-order/whitespace invariant;
    text verbatim incl. newline deltas); `content_bytes`
    is the sole hashed/written artifact.
13. `SourceRefSpec` composes the EXISTING `AuditPolicy`
    (retention, on `ScheduleSpec.audit`) +
    `LiveSourceCachePolicy` (cache) — NO duplicate
    policy model; only `live_change_policy` +
    `explicit_default` added. `InputSpec.source_ref`
    additive + hash-neutral when None.
14. Full v2 test suite green (1888 baseline + phase-10
    adds); no regressions.
15. Annotated tag `v2-phase-10-complete` created (push
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
descriptors. Per-fire snapshot writer persists the
canonical content_bytes VERBATIM to a raw file
data/contract_audit/<schedule>/<run>/sources/<id>.bin
(no JSON envelope; sha256(file)==content_hash) + a
source_snapshots row, content-addressed, dedup-by-hash,
with per-ScheduleSpec retention (AuditPolicy) that
commits row deletes BEFORE post-commit best-effort file
unlink (no rollback data loss) + an explicit on_oversize
branch (no silent fallback). Per-source cache + fallback
mirrors the registry-cache load/stale shape; exactly one
typed SourceError subclass (SourceFetchError) is
fallback-eligible — auth / security / policy / parse
failures never cache or fall back. Live-change policy
detects item_count / schema shape drift and routes
allow / alert / re-approve. resolve_source orchestrates
dispatch → cache → snapshot → drift → exactly one
SOURCE_RESOLVED / SOURCE_DRIFT_DETECTED / SOURCE_FAILED
event.

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

## 9a. Codex round-2 disposition (CLOSED)

Round-2 was a plan↔design drift problem. All 4🔴 + 1🟡:

- **🔴 design §5.3.1 drift** — `docs/CONTRACTS_V2_DESIGN.md`
  §5.3.1 still said `realpath()+startswith(root prefix)`,
  reintroducing the sibling-prefix bypass. Rewritten to
  `Path.resolve(strict=True)+is_relative_to(root.resolve())`,
  identical to §3.2, with an explicit "plan↔design
  consistency is load-bearing, CI-pinned" clause. Re-grep
  of the design doc for `realpath`/`startswith`/`prefix`/
  fence wording → only §5.3.1 (line 22 "prefix blocks" is
  unrelated changelog text; line 1633 is a Slack
  registry allowlist, unrelated). No other drift.
- **🔴 fence reject is non-fallback** — split
  `SourceSecurityError` (§3.5): path-fence / deny-list /
  symlink / traversal rejects raise it; NON-fallback →
  `SOURCE_FAILED`. A denied read can never serve a
  last-good snapshot. §3.2 raises it.
- **🔴 oversize fail_and_alert non-fallback** — new
  `SourcePolicyError` (§3.5); §3.4 `on_oversize`
  `fail_and_alert` raises it (was `SourceFetchError`).
  The resolver rule is now a SINGLE predicate
  (`fallback_eligible` ClassVar, True only on
  `SourceFetchError`) so there is no ambiguity and no
  per-subclass enumeration to drift.
- **🔴 unlink-inside-transaction data loss** — §3.4
  reordered: row DELETEs COMMIT first; file unlink is
  best-effort AFTER commit on a fresh connection with a
  re-query; orphaned file (harmless) counted in
  `PruneResult.orphans_logged`; deleted-file-with-live-
  row is now structurally impossible.
- **🟡 on-disk format** — §1.2/§3.6: the snapshot file
  is `<source_id>.bin` holding `content_bytes` VERBATIM
  (no JSON envelope); `sha256(file) == content_hash`;
  metadata in the `source_snapshots` row only. Pinned
  per declared kind incl. binary.

No open questions remain. Plan-review round 3 requested.

## 9b. Codex round-3 disposition (CLOSED) — single reconciliation pass

Round-3 was 3🔴, ALL the same drift class: plan body
correct, `docs/CONTRACTS_V2_DESIGN.md` §5.3.5 + the §8
tag-annotation lagging. Closed in ONE reconciliation
pass so the round-by-round drift ends here.

- **🔴 design snapshot file** — `CONTRACTS_V2_DESIGN.md`
  §5.3.5 on-disk bullet: `<source_id>.json` →
  `<source_id>.bin` (raw `content_bytes` verbatim, no
  envelope, `sha256(file)==content_hash`, metadata in
  the row only). Verbatim-matches plan §1.2 / §3.6.
- **🔴 design prune sequence** — §5.3.5 step 3-4
  rewritten: COMMIT row deletes FIRST, then post-commit
  re-query + best-effort unlink on a fresh connection;
  orphan harmless + counted; deleted-file-with-live-row
  structurally impossible. Verbatim-matches plan §3.4.
- **🔴 §8 tag-annotation** — `sources/<id>.json` →
  `.bin` raw `content_bytes`; added the corrected
  prune-order + single-`fallback_eligible` wording. This
  block ships as the permanent annotated-tag record, so
  it now matches the final spec.

**Reconciliation diff run (single pass):** grep over
BOTH docs for `json` / `unlink` / `transaction` /
`<source_id>` / `snapshot-file` / `realpath` /
`startswith`, every hit classified:
- snapshot file = `.bin` everywhere (plan §1.2/§3.2/§3.4
  /§3.6/§5/§7/§10/§8-tag; design §5.3.5) — CONSISTENT.
- prune = commit-row-deletes-then-post-commit-unlink
  everywhere (plan §3.4; design §5.3.5) — CONSISTENT.
- fence = `Path.resolve(strict=True)+is_relative_to`
  everywhere (plan §3.2; design §5.3.1) — CONSISTENT;
  the lone `realpath/startswith` in each doc is the
  explicit "Do NOT use" anti-pattern callout.
- residual `.json`: design L871 `slack_channels.json`
  (registry cache) + L1252 `data/contracts/*/v*.json`
  (v1 migration) — UNRELATED to source snapshots.
- residual `realpath/startswith` in plan: dated
  round-1/2/3 disposition narrative (accurate history).

**Zero substantive divergences remain. plan ≡ design ≡
tag-annotation.** Plan-review round 4 requested.

## 9. Codex round-1 disposition (CLOSED)

All round-1 findings + Q-calls applied in this plan-fix.

**🔴 fixes:**
- **Local-file fence sibling-prefix bypass** — §3.2: the
  fence is now `Path(p).resolve(strict=True)` +
  `is_relative_to(root.resolve())` (separator-aware,
  symlink-resolving); the `startswith` bug is explicitly
  called out + pinned by a sibling-prefix +
  symlink-escape + symlink-into-denylist test (§5).
- **Auth vs fetch error contract** — §3.5 typed
  `SourceError` hierarchy (`SourceAuthError` /
  `SourceFetchError` / `SourceParseError`). Resolver
  routes auth → `SOURCE_FAILED` with cache AND fallback
  bypassed; only `SourceFetchError` is fallback-eligible.
  Pinned by the warm-cache auth-bypass test (§5).
- **Retention semantics** — §3.4: `prune_snapshots`
  DELETES rows + backing files, content-hash-shared-file
  safe, scoped by `schedule_id` via a `runs` join; a NEW
  sanctioned storage mutation; the
  `source_snapshots.py` "no delete surface" docstring is
  amended (insert append-only; prune retention-only) and
  a `docs/CONTRACTS_V2_DESIGN.md` §5.3.5
  retention-MECHANISM clause lands in this plan-fix
  commit.

**🟡 fixes:**
- **Canonical bytes** — §3.6: per-type canonical encoding
  (`SourceResult.content_bytes`) is the sole
  hashed/written artifact; JSON canonicalised, text/yaml
  verbatim, binary opt-in. Hash-stability pinned (§5).
- **No duplicate policy model** — §1.8: `SourceRefSpec`
  COMPOSES the existing `AuditPolicy` (retention, stays
  on `ScheduleSpec.audit`) + `LiveSourceCachePolicy`
  (cache); only the genuinely-missing
  `live_change_policy` + `explicit_default` are added.
  No `SnapshotRetentionPolicy`.

**Q-call answers (folded in):**
- **Q1** — `SourceRefSpec` yes; reuse existing
  `AuditPolicy` + `LiveSourceCachePolicy`; no duplicate
  retention model. (§1.8)
- **Q2** — two DISTINCT cache knobs, both pinned:
  `InputSpec.cache_for_seconds` = in-wakeup-window memo;
  `LiveSourceCachePolicy.cache_ttl_seconds` = cross-fire
  source cache. (§1.4)
- **Q3** — Protocol DI for the Slack/Drive readers
  (`SlackThreadReader` / `DriveFileReader`), mirroring
  phase-9 `SlackProtocol`; no vendor SDK at module load.
  (§3.1, §10.4)
- **Q4** — keep the design root
  `data/contract_audit/<schedule_id>/<run_id>/sources/<source_id>.bin`
  (round-2 🟡: `.bin` raw `content_bytes`, not `.json`);
  the local-file deny-list STILL blocks reads under
  `data/contract_audit/` (a source can never read the
  snapshot audit tree). RUNBOOK coherence note carried
  to closeout. (§1.2, §3.2)
- **Q5** — keep 9 slices; the security-heavy `local_file`
  (slice 3) and `cache` (slice 5) get separate codex
  review, NOT bundled. (§4)

No open questions remain. Plan-review round 2 requested.

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
11. Loaders signal failure ONLY via the §3.5 typed
    `SourceError` hierarchy — never a bare `Exception` /
    `return None`. EXACTLY ONE subclass
    (`SourceFetchError`) is `fallback_eligible`; every
    other (`SourceAuthError` / `SourceSecurityError` /
    `SourcePolicyError` / `SourceParseError`) routes
    straight to `SOURCE_FAILED` with cache AND fallback
    bypassed. The base default is non-fallback (fail-safe).
12. `content_hash` + the on-disk `<source_id>.bin` file
    are derived from `SourceResult.content_bytes` (§3.6)
    ONLY — the file holds `content_bytes` VERBATIM (no
    envelope) so `sha256(file) == content_hash`; never
    re-serialize `content`.
13. Retention (`prune_snapshots`) is the SOLE sanctioned
    `source_snapshots` delete; `insert_snapshot` stays
    append-only + content-addressed. Row DELETEs COMMIT
    before any file unlink; a shared file is unlinked
    (best-effort, post-commit) only when its last
    referencing row is pruned. Orphaned file ≠ data loss;
    deleted file with a live row is structurally
    impossible.
14. Intentional workspace dirt left alone:
    `app/tools/youtube.py` + `.playwright-mcp/` + the 3
    `scripts/diag_*` / `amazon_ads_mcp_proxy.py`
    untracked.
