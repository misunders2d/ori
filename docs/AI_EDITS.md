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

## 10. Every product change MUST land in the docs in the same commit

Docs are 2-tier. BOTH tiers are non-optional when you ship behavior.

### 10a. Auto-generated docs (factual: which symbols exist)

If you add or remove a sub-agent, tool, toolset, callback, or skill, run:

```
uv run python scripts/gen_docs.py
```

This regenerates `docs/INDEX.md`, `docs/AGENTS_INVENTORY.md`, `docs/TOOLS.md`, `docs/TOOLSETS.md`, and `docs/CALLBACKS.md` from an AST scan. The pre-commit hook auto-runs this and stages the resulting diffs, so a clean `git commit` keeps these in sync without you thinking about it. `evolution_verify_sandbox` runs the same script before pytest.

### 10b. Hand-written docs (design intent: WHY the code is shaped this way)

Auto-generation cannot write prose. The following docs hold the design rationale, usage patterns, gotchas, and incident history — they must be updated **by hand, in the same commit as the code change**, whenever you ship user-visible behaviour:

| If you change… | Update |
|---|---|
| A2A protocol, friend / DNA flow, file transfer, SSRF guards | `docs/A2A.md` |
| Model selection, provider routing, hot-swap, thinking config | `docs/HOT_SWAP.md` |
| Self-evolution flow, sandbox lifecycle, supervisor contract | `docs/EVOLUTION.md` |
| Plan schema, `plan_enforcer`, `plan_step_enforcer` | `docs/PLANS.md` |
| Scratchpad shape, ownership, retention | `docs/SCRATCHPAD.md` |
| Contract pipeline (`schedule_contract`, loaders, emit adapters) | `docs/CONTRACTS.md` |
| Production deploy, rollback, recovery procedures | `docs/RUNBOOK.md` |
| Any new hard rule for AI editors | `docs/AI_EDITS.md` (this file) |

Triggers that ALWAYS require a hand-written doc update:

- A new callback registered on any agent
- A new env knob, threshold, or feature flag
- A new failure mode or `on_failure` action
- A new guardrail (after-tool / before-model behaviour change visible to the agent)
- A schema change that affects what the agent or user can express
- A new automatic side effect (auto-repair, auto-schedule, auto-attach, auto-anything)

The rule is "would another agent reading this codebase later understand WHY this exists?". If the answer needs prose, that prose belongs in the matching hand-written doc, NOT just in a docstring or a commit message.

This rule was added after the 2026-05-11 round of fixes (auto-repair, global thinking switch, file-attachment plumbing) shipped code without touching `docs/HOT_SWAP.md` or `docs/A2A.md` — the auto docs covered the symbols, but the design rationale silently went undocumented. Don't repeat that. The user spotted the gap; the rule prevents it next time.

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

---

## When the rules don't fit

If a legitimate change conflicts with one of these rules, **say so explicitly and stop** — don't paper over the gap, don't write a workaround that subverts the rule. The right move is to surface the conflict to the human admin and let them either bless an exception or reshape the request.

Examples of correct behavior:
- "Rule 3 forbids editing `deploy/`. The task as described needs a change there. I'm stopping and surfacing this — you'll need to apply it manually or amend the rule."
- "Rule 2 says extend existing toolset. The closest existing one is `KeepaToolset` but the requested functionality is structurally different. Proposing a new toolset — see `evolutions/<name>/EVOLUTION.md`."

Silent rule-violation is worse than failing the task. Future agents (and humans reviewing diffs) need to trust that these guardrails actually held.
