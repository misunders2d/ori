# Claude Code / external AI orientation

You are likely Claude Code (or another external AI agent) working on the **Ori** repository — a self-evolving Google ADK 1.x agent platform.

## 🛑 Hard-gated requirement before ANY commit

A pre-commit hook (`.githooks/pre-commit`) blocks `git commit` unless a fresh `.docs_read_marker` file is present. To create it:

```
uv run python scripts/check_docs_read.py
```

This prompts you to confirm you've read `docs/AI_EDITS.md` + `docs/INDEX.md`, then writes the marker (valid for 2 hours).

The marker is in `.gitignore` so it never crosses clones. **Do NOT** bypass with `git commit --no-verify` — it's forbidden by `docs/AI_EDITS.md` rule 4 and leaves no audit trail.

If the hook isn't firing, the installer probably hasn't run yet for this clone:

```
uv run python scripts/install_hooks.py
```

(also run automatically by the bot's startup + supervisor boot sequence).

## Reading order before any edit

1. **[docs/AI_EDITS.md](docs/AI_EDITS.md)** — 12 hard rules. The most important is rule 1: search `docs/INDEX.md` for what you're about to build; it almost certainly already exists.
2. **[docs/INDEX.md](docs/INDEX.md)** — auto-generated map of every sub-agent, tool, toolset, callback, skill (with `file:line` refs).
3. **[AGENTS.md](AGENTS.md)** — the constitutional manifesto (philosophy, immutable laws, architectural ethos).

For the area you're touching, read the matching topic doc:

| If you're editing... | Read |
|---|---|
| A sub-agent | `docs/AGENTS_INVENTORY.md` |
| A tool / toolset | `docs/TOOLS.md`, `docs/TOOLSETS.md` |
| A callback / guardrail | `docs/CALLBACKS.md` |
| A2A protocol | `docs/A2A.md` |
| Self-evolution flow | `docs/EVOLUTION.md` |
| Model assignments | `docs/HOT_SWAP.md` |
| Plan / scheduler | `docs/PLANS.md` |
| Scratchpad | `docs/SCRATCHPAD.md` |
| Production / deploy / disaster recovery | `docs/RUNBOOK.md` |

After structural changes (new sub-agent, tool, toolset, callback, skill), regenerate the index:

```
uv run python scripts/gen_docs.py
```

The self-evolution pipeline does this automatically before pytest, but if you're editing outside that path (e.g. as Claude Code on the host), do it by hand so the index stays accurate.

## Python runs

This project is uv-managed. **Always** invoke Python through `uv run` — `uv run python -m pytest`, `uv run python scripts/<x>.py`, `uv run python run_bot.py`. Never `.venv/bin/python`, `python3`, or system `python` directly. See `docs/AI_EDITS.md` §11.

## Branch + worktree note (lessons from 2026-05-11)

This repo is often a **git worktree**. Always run `git worktree list` and `git status -sb` before any reset/pull/push. The May 2026 incident was caused by treating two sibling worktrees as the same checkout. Don't reproduce it.

If you face a non-fast-forward / divergent-branch error, **do not** `git reset --hard origin/<branch>` until you have:

1. Tagged the current state: `git tag rescue-$(date +%s)`.
2. Pushed any local-only branches to origin.
3. Inspected `git reflog show HEAD | head -50` to confirm what you're about to discard.

`docs/RUNBOOK.md §6` has the full recovery procedure.

## Caveman mode hint

If the user invokes `/caveman` or asks for terse output, comply with the caveman skill conventions: drop articles/filler, fragments OK, code blocks unchanged, errors quoted verbatim. The user uses this regularly to reduce token usage.
