# Self-Evolution

How Ori modifies its own code under guardrails. The agent is allowed to extend, fix, or rewrite parts of itself, but never without going through this pipeline.

> Source files: `app/tools/evolution.py` (982 lines), `app/tools/evolution_catalog.py`, `app/tools/system.py`, `app/callbacks/guardrails.py:93-200` (admin staging), `app/core/pending_actions.py`.

---

## 1. The cycle

```
read → stage → verify → (approval) → commit → exit-signal → restart
```

Every step is a tool the agent can call. The pipeline is enforced by the verification + approval gates — skipping a step means the commit refuses to run.

### Tool surface

| Tool | What it does |
|---|---|
| `evolution_read_file(path)` | Read a source file (path-traversal + `.env*` guards in place — see `evolution.py:56-76`) |
| `evolution_list_directory(path)` | Explore project structure |
| `evolution_stage_change(path, content)` | Write the proposed new contents to `data/sandbox/<path>` (does NOT touch the live tree) |
| `evolution_verify_sandbox(check=syntax\|deps\|pytest\|all)` | Run checks on the sandboxed contents. Updates the digest used by `commit_and_push`. |
| `evolution_commit_and_push(message)` | Commit + push. Admin-staged via `admin_tool_guardrail` (returns ACT-* token) — needs explicit human approval + TOTP before the action actually fires. |
| `evolution_discard_sandbox` | Abort the cycle. Clears `data/sandbox/` and its marker. |
| `evolution_git_pull`, `evolution_git_reset`, `evolution_git_fetch`, `evolution_git_log`, `evolution_git_diff_summary`, `evolution_git_diff_file`, `evolution_git_branches` | Read-side git ops; pull / reset are also admin-staged. |

---

## 2. Sandbox lifecycle

### Path
`./data/sandbox/` — mirrors the project tree. A file at `app/foo.py` in the live tree, when staged, lives at `data/sandbox/app/foo.py`.

### Marker
After Phase 2 of the hardening plan, the cycle marker lives at `data/sandbox/.cycle_active` (file mtime = cycle start time). Helpers:

- `_sandbox_cycle_begin(sandbox_dir)` — wipe + recreate + mark
- `_sandbox_cycle_is_fresh(sandbox_dir)` — true if marker exists AND mtime ≤ TTL (default **24 h**)
- `_sandbox_cycle_end(sandbox_dir)` — wipe + remove marker (called by `evolution_commit_and_push` on success, `evolution_discard_sandbox`, and the rollback path)

Why a disk marker instead of session state: an interrupted evolution (process restart, `/reset session`, container redeploy) should not silently lose the in-flight stage. The disk marker survives all of those; the session-state marker the old code used did not, and that bug bit us in production.

### Hard write-blocks
The sandbox refuses to accept stages for any of:

- `deploy/**`
- `data/**` (except `data/sandbox/**` itself)
- `.env*`
- `*.bak` / `*.sql` / vault files

See `evolution.py:148-150`.

---

## 3. Verification

`evolution_verify_sandbox` runs in three modes (or `all`):

| Mode | What it checks |
|---|---|
| `syntax` | `ast.parse` every staged `.py` file |
| `deps` | `uv lock` / `uv sync --frozen` against the staged `pyproject.toml` |
| `pytest` | Full test suite, executed against the **merged** view (live tree + sandbox overlay). Failures block the commit. |

A passing pytest run updates the digest hash (`evolution.py:363-364`). The commit step compares this digest against the current sandbox state; if anything changed after verification, the commit refuses. This catches an agent that stages → verifies → re-stages something different → commits, hoping the verification still applies.

Hardening plan adds:
- A `gen_docs.py` step before pytest (Phase 1), so the docs index reflects the staged structure.
- An evolution audit log entry per check (Phase 5).

---

## 4. Approval

`evolution_commit_and_push`, `evolution_git_pull`, `evolution_git_reset`, `evolution_sync_local_to_upstream`, `update_self`, `trigger_rollback`: all wrapped by `admin_tool_guardrail` (`app/callbacks/guardrails.py:93-200`).

Flow:

1. Agent calls the protected tool.
2. `admin_tool_guardrail` intercepts. If caller is not in `ADMIN_USER_IDS`, returns a refusal that the agent must relay.
3. If caller is admin, the tool call is *staged* into `data/pending_actions.db` with a 15-minute TTL. Tool returns a token `ACT-XXXXXX` rather than executing.
4. The admin then runs `execute_approved_action(ACT-XXXXXX, totp_code)` (`app/tools/system.py:126-174`):
   - Validates the token still exists.
   - Validates `user_id` / `session_id` match the original staging.
   - Verifies `totp_code` against `ADMIN_TOTP_SECRET` (skippable only if `REQUIRE_2FA=false` is set in vault).
   - Atomically retrieves + deletes the staged action.
   - Executes the original tool with the staged args.

Approval state lives in SQLite (`data/pending_actions.db`), so it survives restarts; expired entries are cleaned via `pending_actions.cleanup_expired()` (called on-demand, not auto-scheduled).

---

## 5. Commit paths

Two flavors implemented in `evolution.py`:

### Remote (`_evolution_commit_remote`, lines 412-471)

1. Clone the GitHub remote into a tmp dir.
2. Apply staged files over the clone.
3. Commit + push to master.
4. Update the local tree from the remote.

Used when the bot deploys from a clean checkout.

### Local worktree (`_evolution_commit_local`, lines 474-562)

1. `git worktree add data/evo-work evo/<uuid8>` — fresh worktree on a new branch.
2. Apply staged files.
3. Commit on the new branch.
4. Merge into `master` (still inside the worktree).
5. `git worktree remove data/evo-work`; delete the branch.

The live tree is **not touched** until the exit signal fires and the supervisor restarts the container from the merged `master`. Failures clean up the worktree + branch.

### Auto-resolve deps (lines 620-658)

If `pyproject.toml` is part of the stage, `uv lock` + `uv sync --frozen` run before the commit. The updated `uv.lock` is included in the commit.

---

## 6. Exit signal + restart

After a successful commit, `_write_exit_signal(EXIT_CODE_UPDATE)` writes `data/.exit_signal` with a code byte. The transport layer polls this file after every user response and, when set, calls `sys.exit(EXIT_CODE_UPDATE)` cleanly. The supervisor (Docker / systemd) sees the exit code and restarts the container from the freshly merged tree.

`trigger_rollback` is the opposite signal: it writes `EXIT_CODE_ROLLBACK`, which the supervisor interprets as "redeploy the previous tag." No code path uses `os._exit` directly — that races against pending file writes (see `app/tools/system.py:1-7`).

---

## 7. Catalog (DNA library)

`app/tools/evolution_catalog.py` indexes evolution bundles in `evolutions/<slug>/`. Each slug holds:

```
evolutions/<slug>/
├── EVOLUTION.md       # frontmatter + description
├── files/             # staged code (mirrors the project tree)
├── tests/             # accompanying tests
└── manifest.json      # name, description, file_paths, tags, usage_notes
```

Tools:

- `evolution_catalog(name, description, file_paths, tags, usage_notes)` — register the current sandbox as a re-usable bundle.
- `evolution_search(query)` — find bundles by keyword across name / description / tags.
- `evolution_share(slug)` — package a bundle for A2A delivery.
- `evolution_import(friend_nickname, slug)` — fetch a bundle from a friend; **does not apply** — drops it in the sandbox for verification.

Today `evolutions/` contains only `.gitkeep`; populating it is part of Phase 5 of the hardening plan.

---

## 8. Tests

| Test | Covers |
|---|---|
| `tests/test_evolution_security.py` | Path-traversal rejection (`evolution.py:35-41, 162-165`), `.env` write-block, can't bypass sandbox staging, can't write to `deploy/` |
| `tests/test_evolution_hygiene.py` | Verification flow (syntax/deps/pytest contracts) |
| `tests/test_evolution_e2e.py` (Phase 5, new) | Full stage → verify → commit → exit-signal cycle. Marked `pytest.mark.local_only` — needs a real git remote |

---

## 9. What you cannot evolve

- `deploy/`, `data/`, `.env*`, vault files (rule 3 in `AI_EDITS.md`)
- Anything that would change the approval flow itself (would require a hand-edit + manual commit by the admin)
- The evolution toolchain's own protection rules (same — the agent can propose changes, but the commit must be hand-approved by the admin, who is expected to read the diff carefully)

---

## 10. If something goes wrong mid-cycle

| Situation | Recovery |
|---|---|
| Verification fails | `evolution_discard_sandbox` then start over. Failures aren't expensive. |
| Commit fails (e.g. push rejected, conflict) | Local-commit path rolls back; remote path leaves the local tree untouched. Inspect with `evolution_git_log`. |
| Restart loop after commit | Container restarts but immediately exits again — usually a syntax error escaped verification. Use `trigger_rollback` from the supervisor (or `git revert <commit>` on the host). |
| Marker stale (`> 24 h`) but sandbox still present | The cycle is considered abandoned. Next `evolution_stage_change` wipes it implicitly via `_sandbox_cycle_begin`. |
| Sandbox dir got hand-edited | Run `evolution_discard_sandbox` to nuke + reset. Manual edits inside `data/sandbox/` won't survive the next stage anyway. |
