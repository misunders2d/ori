# Claude Code / external AI orientation

You are likely Claude Code (or another external AI agent) working on the **Ori** repository — a self-evolving Google ADK 1.x agent platform.

Before you edit anything, read these in order:

1. **[docs/AI_EDITS.md](docs/AI_EDITS.md)** — 10 hard rules. The most important is rule 1: search `docs/INDEX.md` for what you're about to build; it almost certainly already exists.
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
python scripts/gen_docs.py
```

The self-evolution pipeline does this automatically before pytest, but if you're editing outside that path (e.g. as Claude Code on the host), do it by hand so the index stays accurate.

## Branch + worktree note (lessons from 2026-05-11)

This repo is often a **git worktree**. Always run `git worktree list` and `git status -sb` before any reset/pull/push. The May 2026 incident was caused by treating two sibling worktrees as the same checkout. Don't reproduce it.

If you face a non-fast-forward / divergent-branch error, **do not** `git reset --hard origin/<branch>` until you have:

1. Tagged the current state: `git tag rescue-$(date +%s)`.
2. Pushed any local-only branches to origin.
3. Inspected `git reflog show HEAD | head -50` to confirm what you're about to discard.

`docs/RUNBOOK.md §6` has the full recovery procedure.

## Caveman mode hint

If the user invokes `/caveman` or asks for terse output, comply with the caveman skill conventions: drop articles/filler, fragments OK, code blocks unchanged, errors quoted verbatim. The user uses this regularly to reduce token usage.
