# AI Edit Rules

Anyone modifying this repo — Ori's own sub-agents, Claude Code sessions, or any other automated editor — reads this **first**. If your change cannot satisfy these rules, do not make it; surface the gap and stop.

The rules are intentionally short and blunt. The longer "why" lives in `AGENTS.md` (the manifesto) and the topic-specific docs (`docs/EVOLUTION.md`, `docs/A2A.md`, etc).

> **Rules 1 and 10 are now load-bearing — not honor-system.**
>
> - **Internal `DeveloperAgent`**: `evolution_stage_change` refuses with `{"status": "needs_docs_read"}` until BOTH `evolution_read_file("docs/AI_EDITS.md")` and `evolution_read_file("docs/INDEX.md")` have run in the same session. Helper: `_doc_read_state()` in `app/tools/evolution.py`.
> - **External AI (Claude Code, etc.)**: `.githooks/pre-commit` blocks `git commit` until `.docs_read_marker` exists (created by `uv run python scripts/check_docs_read.py`, valid 2 h). Installer: `uv run python scripts/install_hooks.py` (also runs from `run_bot.py` startup).
>
> Bypass via `git commit --no-verify` is forbidden by rule 4 and leaves no audit trail.

---

## 1. Read the index first

Open `docs/INDEX.md` and search for whatever you're about to build. Most of the time it already exists as a tool, a toolset, a skill, or a callback. Reuse beats reinventing every single time.

If it exists but is misnamed/mis-located: file a note in the relevant doc and use the existing thing. Do not silently fork.

## 2. Extend existing toolsets — don't create parallel raw tools

`app/toolsets/*.py` are the canonical agent-facing groupings. If your new functionality is close to one of them (e.g. another Keepa endpoint, another graph query), extend that toolset. Do not write a raw `app/tools/something.py` that the agent has to import explicitly when a toolset already covers the area.

Overlaps that already exist (do not extend further — they should converge over time): `graph_tools.py` + `graph.py`, `keepa_api.py` + `keepa.py`, `sp_api_tools.py` + `sp_api.py`.

## 3. No edits to `deploy/`, `data/`, `.env*`, or `vault/` from any evolution path

These are immutable to the agent's self-modifying machinery. The block is hard-coded in `app/tools/evolution.py:148-150`. Don't try to route around it; surface the requirement to a human admin instead.

## 4. No `os._exit()` / `sys.exit()` in code paths

Use the exit-signal pattern: write `data/.exit_signal` and let the supervisor drain + restart. `app/tools/system.py:_write_exit_signal` is the entry point. Direct exits race against file writes and break the sandbox cycle.

## 5. Callbacks live in `app/callbacks/`

ADK 1.x callback hooks: `before_model` (signature `callback_context, llm_request`), `before_tool` / `after_tool` (signature `tool, args, tool_context[, tool_response]`). New callbacks register on the agent that needs them (`agent.before_model_callback=[...]` etc). Do not introduce a parallel plugin system; ADK 1.x doesn't have one.

When more than one callback runs on the same hook, they run in **list order**. Mutations to `llm_request` / `tool_response` are visible to later callbacks. Document ordering constraints in `docs/CALLBACKS.md` (the auto-gen part lists them; add an "ordering" note by hand below it).

## 6. Plan-aware tools require `confirmed=True` on destructive ops

After Phase 3 lands, when an active plan exists in the session, `plan_step_enforcer` will hard-block any tool that isn't in `current_step.allowed_tools`. Destructive Neo4j ops (`delete_record`, `delete_person`, `delete_entity`, `merge_persons`) require an explicit `confirmed=True` argument — without it they return `{"status": "needs_confirmation", "preview": ..., "impact": ...}` and the agent must relay the preview to the user before retrying with the flag set.

Catastrophic admin ops (e.g. `update_self`, `trigger_rollback`, `evolution_commit_and_push`) still go through the ACT-token + TOTP flow staged by `admin_tool_guardrail`. That gate is separate from the per-op `confirmed=` flag.

## 7. Tests required for every new tool / callback / toolset

`uv run python -m pytest tests/` must pass before any commit. The self-evolution path enforces this via `evolution_verify_sandbox(check="pytest")` — a stage that fails verification cannot proceed to commit.

If your change is to a tool, add at least:
- A success-path test (happy case, expected return shape).
- A failure-path test (bad inputs, missing dependency, etc — returns `{"status": "error", "message": ...}`).
- An ACL test if the tool is creator-gated or admin-gated.

## 8. Sandbox-first for self-evolution

Evolution stages go to `data/sandbox/` and must clear `evolution_verify_sandbox` (syntax + imports + pytest, see `app/tools/evolution.py:181-381`) before `evolution_commit_and_push` will run. The cycle marker lives at `data/sandbox/.cycle_active` (after Phase 2) — it survives session resets and process restarts, so an interrupted evolution can be resumed instead of restarted.

If you abandon an evolution, call `evolution_discard_sandbox` so the marker and staged files are cleared. Do not just delete the directory by hand; the audit log (Phase 5) expects a paired begin/end event.

## 9. Models, providers, hot-swap

Use `get_model(component)` from `app/app_utils/models.py`. Do not hardcode model strings. The resolution order is `os.environ[MODEL_<COMPONENT>]` → `data/model_config.json` → `MODEL_DEFAULTS`. After Phase 2, the env is rehydrated from disk at process start, so hot-swap survives restart.

Adding a new model component? Register it in `VALID_COMPONENTS` (`app/app_utils/models.py`) so `/models set` accepts it and the docs index picks it up.

## 10. Code changes ship with docs (enforced by the pre-commit hook)

Two-tier docs. Auto-gen handles SYMBOLS (`gen_docs.py` regenerates INDEX/AGENTS_INVENTORY/TOOLS/TOOLSETS/CALLBACKS at commit time). Hand-written holds DESIGN (A2A, HOT_SWAP, EVOLUTION, PLANS, SCRATCHPAD, CONTRACTS, RUNBOOK).

The pre-commit hook rejects any commit that touches behaviour-relevant code (`app/callbacks/`, `app/tools/`, `app/toolsets/`, `app/sub_agents/`, `app/contracts/`, `app/app_utils/models.py`, `app/agent.py`) without staging at least one `docs/*.md` change in the same commit. Trivial / mechanical changes (typos, comment edits, lint pass) bypass with `ORI_SKIP_DOC_CHECK=1 git commit ...`.

## 11. Run Python via `uv`, never directly

This project is uv-managed (lockfile in `uv.lock`, `pyproject.toml` is the source of truth). All Python invocation goes through `uv run`:

```
uv run python -m pytest tests/
uv run python scripts/gen_docs.py
uv run python run_bot.py
uv run python -m py_compile app/tools/evolution.py
```

**Do not** call `.venv/bin/python`, `python3`, or system `python` directly. Bypassing uv can pick up a stale interpreter or wrong site-packages and produce results that don't match the locked environment. If you find an existing command in a doc or script that uses raw `python`, fix it to use `uv run python`.

## 12. Trust the supervisor — don't issue manual `uv sync` / `git pull` / `docker build`

`deploy/ori-supervisor.py` already does these for you. It hashes `pyproject.toml` + `uv.lock` into `data/.deps_hash` and runs `uv sync --frozen` on mismatch every boot. It pulls the current branch (worktree-aware — not hardcoded `master`) and rebuilds the child Docker image on evolution exit (signal 100). It reverts one commit on rollback exit (101). Full contract in `docs/RUNBOOK.md` §2.

`--frozen` is load-bearing: it forbids `uv` from rewriting `uv.lock` on deploy hosts. A bare `uv sync` mutates the lockfile, which then blocks the next `git pull` with a merge conflict (2026-05-11 contabo incident). Lock updates happen only on a dev box, only as part of a commit. **Never** run `uv sync` (without `--frozen`) on a deploy host — and never tell the user to.

When telling the user how to update production, the answer is almost always **`git pull && ./deploy/start.sh`** — never "and then run `uv sync` too." Pre-empting the supervisor is redundant noise and trains the user to bypass the safety mechanisms baked into the supervisor.

If you genuinely need to force a re-sync (e.g. corrupt `.venv`), the documented escape hatch is `echo > data/.deps_hash && ./deploy/start.sh` — invalidate the fingerprint, let the supervisor handle the rest.

## 13. Nothing fails silently (architectural axiom)

Every error path MUST reach at least one of three observable destinations:

1. **Logs.** `logger.error(...)` minimum; `logger.critical(...)` for FATALs that warrant operator action. Lands in `journalctl` / `data/agent.log`.
2. **Agent.** Tool returns `{"status": "error", "message": "..."}` that the calling agent surfaces VERBATIM to the user. LLMs MUST NOT swallow error responses or summarize a failed call as "task complete".
3. **Admin channel.** Telegram DM to `ADMIN_USER_IDS[0]` (or the equivalent Slack admin channel) for any **background-fired** failure where no agent is in the loop to surface the error directly. Scheduled tasks, contract fires, async workers, supervisor restarts, hook failures — all route here.

Forbidden patterns:

- `except: pass` or `except Exception: pass` — always at minimum log; usually re-raise or return an error status.
- `try/except` that returns a fake success on failure (`return {"status": "success"}` in an except block).
- "Fallback" paths that quietly substitute fake/empty data for a failed call. If a fallback CAN'T avoid degrading silently, it MUST emit a CRITICAL log at the moment it engages.
- Bot/agent rephrasing a tool's `status: error` message as "task complete" or "done". The exact error text must reach the user.
- A failure-handler that itself fails silently (e.g. `alert_admin` with empty `notify` list, swallowed delivery errors). The failure of the failure-handler MUST also be logged CRITICAL.

When writing new code: ask "if this raises / returns error, who finds out and how?" before writing the except block. If the answer is "no one", you have a Law 6 violation.

Production proof (2026-05-12): 5 AI Pilot contracts FATALed daily for ~2 weeks because the emit adapter referenced an unknown name → silently caught by `on_failure` → `alert_admin` had empty `notify` → delivered to nobody → no journal CRITICAL line. Three layers of silence stacked. The fix wired (a) freeze-time registry validation, (b) `ADMIN_USER_IDS` fallback in `on_failure`, (c) `logger.critical` on every contract failure. See `app/contracts/worker.py:_on_failure` for the canonical pattern.

---

## When the rules don't fit

If a legitimate change conflicts with one of these rules, **say so explicitly and stop** — don't paper over the gap, don't write a workaround that subverts the rule. The right move is to surface the conflict to the human admin and let them either bless an exception or reshape the request.

Examples of correct behavior:
- "Rule 3 forbids editing `deploy/`. The task as described needs a change there. I'm stopping and surfacing this — you'll need to apply it manually or amend the rule."
- "Rule 2 says extend existing toolset. The closest existing one is `KeepaToolset` but the requested functionality is structurally different. Proposing a new toolset — see `evolutions/<name>/EVOLUTION.md`."

Silent rule-violation is worse than failing the task. Future agents (and humans reviewing diffs) need to trust that these guardrails actually held.
