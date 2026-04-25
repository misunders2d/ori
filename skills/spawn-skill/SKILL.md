---
name: spawn-skill
description: "How to spawn disposable child agents in Docker for sandboxed work — iteration, testing, evolution from a clean slate. Children stage and export DNA back; they cannot commit or reboot. Load this when the user asks to test, sandbox, isolate, spawn, or experiment."
---

# Child Agent Spawning

Children are full copies of the parent running in Docker, with one critical difference: **no git tools, no commit, no reboot**. They iterate in isolation, verify their own work, and `export_dna` back to the parent for commit.

## Tools

- `spawn_agent(purpose, config=None)` — launch a new disposable child container. Returns the child's session id.
- `list_spawned_agents()` — list active children with status.
- `stop_spawned_agent(session_id)` — kill a child container.

## When to use

| Scenario | Use child? |
|---|---|
| Building a new feature from scratch | YES — iterate in isolation, export when ready |
| Risky refactor that might break the bot | YES — break the child instead |
| Validating a fix against a clean codebase | YES |
| Adding 1-line config tweak | NO — overhead exceeds benefit |
| Reading code, answering questions | NO — your own context handles this |

## How children work

Children share the parent's image (so they auto-inherit code changes after each parent commit). On spawn:

1. The parent's `data/` directory is **not** mounted; the child has its own per-session sandbox at `data/sandbox/<session_id>/`.
2. The child has all skills/tools EXCEPT `evolution_git_*`, `evolution_commit_and_push`, and `update_self`.
3. The child stages and verifies. When ready, it calls `export_dna(source_paths)` which packs files into a tarball and sends them back to the parent via A2A binary content.
4. The parent receives via `import_dna` (KnowledgeAgent), verifies again in its own sandbox, then commits via the normal evolution-workflow.

You are automatically the admin of any child you spawn — they recognize your `ADMIN_USER_IDS` membership.

## Coordinator-only

The `spawn_agent` tool lives on the Coordinator (so the agent that the user is conversing with can spin up children). DeveloperAgent does not have it directly — Developer asks Coordinator to spawn when sandboxed work is needed.

## Anti-patterns

- Don't spawn a child for a 1-file change that you can stage + verify in your own sandbox.
- Don't expect children to persist beyond their session — they're disposable.
- Don't commit child output blindly — the parent must re-verify in its own sandbox before commit.
