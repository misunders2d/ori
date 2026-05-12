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
| A sub-agent | `docs/AGENTS_INVENTORY.md`, `docs/ROUTING.md` (fallback pattern) |
| A tool / toolset | `docs/TOOLS.md`, `docs/TOOLSETS.md` |
| A callback / guardrail | `docs/CALLBACKS.md` |
| A2A protocol | `docs/A2A.md` |
| Self-evolution flow | `docs/EVOLUTION.md` |
| Model assignments | `docs/HOT_SWAP.md` |
| Per-component thinking levels | `docs/THINKING.md` |
| Plan / scheduler | `docs/PLANS.md` |
| Scheduled tasks (contracts) | `docs/CONTRACTS.md` |
| Scratchpad | `docs/SCRATCHPAD.md` |
| Production / deploy / disaster recovery | `docs/RUNBOOK.md` |

After structural changes (new sub-agent, tool, toolset, callback, skill), regenerate the index:

```
uv run python scripts/gen_docs.py
```

The self-evolution pipeline does this automatically before pytest, but if you're editing outside that path (e.g. as Claude Code on the host), do it by hand so the index stays accurate.

## Python runs

This project is uv-managed. **Always** invoke Python through `uv run` — `uv run python -m pytest`, `uv run python scripts/<x>.py`, `uv run python run_bot.py`. Never `.venv/bin/python`, `python3`, or system `python` directly. See `docs/AI_EDITS.md` §11.

**Do not run `uv sync` manually on deploy hosts** — deploy paths use `uv sync --frozen` so `uv.lock` never drifts. A bare `uv sync` rewrites the lock, blocks the next `git pull`, and is forbidden in production (2026-05-11 contabo incident). Lock updates happen on the dev box, in a commit, only.

## Google ADK is a moving target — check the docs

Ori runs on Google ADK 1.x (currently 1.28). ADK releases breaking changes inside 1.x (import paths, callback signatures, schema converters — we hit one on 2026-05-11 with `additional_properties` in nested `any_of`). When something stops working with an `AttributeError`, `ImportError`, or a Gemini 400 about an unknown field:

1. Check the installed version: `uv pip show google-adk` — pin in `pyproject.toml`.
2. Web-search the **official ADK docs** at `google.github.io/adk-docs/` and the GitHub repo (`google/adk-python`) for the matching version. **Do not trust pre-training knowledge of ADK** — releases since the cutoff may have moved things.
3. Cross-check `skills/google-adk-skill/SKILL.md` for canonical patterns the project relies on (callback ordering, toolset conventions, model wrapper choices).
4. Read `.venv/lib/python3.13/site-packages/google/adk/` directly when docs lag — it's the truth.

The same applies to LiteLLM, OpenRouter routing, and Anthropic / Gemini provider quirks. The 2026-05-11 schema bug surfaced specifically because we assumed ADK's sanitizer covered every nested path; web-checking the upstream would have flagged the gap earlier.

## Branch + worktree note (lessons from 2026-05-11)

This repo is often a **git worktree**. Always run `git worktree list` and `git status -sb` before any reset/pull/push. The May 2026 incident was caused by treating two sibling worktrees as the same checkout. Don't reproduce it.

If you face a non-fast-forward / divergent-branch error, **do not** `git reset --hard origin/<branch>` until you have:

1. Tagged the current state: `git tag rescue-$(date +%s)`.
2. Pushed any local-only branches to origin.
3. Inspected `git reflog show HEAD | head -50` to confirm what you're about to discard.

`docs/RUNBOOK.md §6` has the full recovery procedure.

## Caveman mode hint

If the user invokes `/caveman` or asks for terse output, comply with the caveman skill conventions: drop articles/filler, fragments OK, code blocks unchanged, errors quoted verbatim. The user uses this regularly to reduce token usage.
