---
name: evolution-workflow-skill
description: "The mandatory step-by-step procedure for self-evolution: how to plan, stage, verify, and commit code changes. Different paths for parent (native) vs child (Docker sandbox). Load this any time you're about to modify code."
---

# Evolution Workflow

ONE EVOLUTION = ONE COMMIT = ONE APPROVAL. Never skip a step.

There are two contexts:

- **Parent** (native Python process, has git tools) — full read/write to repo, commits go straight to local master.
- **Child** (Docker sandbox, no git tools) — stage and verify, then `export_dna` back to the parent for commit.

`_is_child_container()` tells you which you are.

## Parent workflow (full git access)

1. **READ** — Understand code/logs before planning. `evolution_read_file`, `evolution_list_directory`, `data/agent.log`.
2. **PULL/CLEAN** — `evolution_git_pull` or `evolution_git_reset` for a fresh workspace. Required before staging.
3. **PLAN** — Explain which files change and why. Be specific. List file paths.
4. **WAIT** — Present the plan to the admin. **FULL STOP** until admin says 'proceed' (or equivalent in any language). This is the ASK FIRST rule.
5. **STAGE** — `evolution_stage_change` for **every** file before moving on. Don't half-stage.
6. **VERIFY** — `evolution_verify_sandbox` with `'syntax'` per file, then `'pytest'`. ALL tests MUST pass. If they don't, fix and re-verify — don't commit a regression.
7. **COMMIT** — `evolution_commit_and_push`. The system writes exit code 100 and the supervisor pulls/syncs/restarts automatically.

## Child workflow (no git)

1. **READ** — Understand code/logs before planning.
2. **PLAN** — Explain which files change and why.
3. **WAIT** — Present plan to admin. FULL STOP until 'proceed'.
4. **STAGE** — `evolution_stage_change` ALL files.
5. **VERIFY** — `evolution_verify_sandbox` with `'syntax'` per file, then `'pytest'`. ALL tests MUST pass.
6. **EXPORT** — `export_dna` to send your verified changes back to the parent agent for commit.

You do NOT have: `evolution_git_pull`, `evolution_commit_and_push`, `evolution_git_reset`, `update_self`. You DO have: catalog tools (`evolution_search`, `evolution_catalog`, `evolution_share`, `evolution_import`).

## Sandboxed evolution (preferred for new features)

When developing a new feature from scratch, ask the coordinator to `spawn_agent` for you. The child:
- Iterates in its own Docker sandbox (no risk to the live parent)
- Verifies its own changes
- `export_dna` back to the parent
- The parent verifies again in its own sandbox
- Then commits

This isolates failures to disposable containers.

## Catalog before building

- **BEFORE building**: `evolution_search` locally, then ask A2A friends for relevant evolutions. Reuse beats reinvent.
- **AFTER committing**: `evolution_catalog` to save the evolution for future reuse.

## Verify rules

- Syntax check fails → fix the file before continuing. Don't "verify later".
- Tests fail → diagnose, fix, re-verify. The 3-failure cap (`VerifyRetryPlugin`) will block further attempts after 3 in a row.
- Tests pass but the feature doesn't work end-to-end → keep iterating. Pytest passing is necessary, not sufficient.

## Anti-patterns

- Don't commit before verify passes. Ever.
- Don't skip the WAIT step. The admin's 'proceed' is the gate.
- Don't extend `.gitignore` to skip a file you want to commit — fix the actual ignore rule, or commit explicitly.
- Don't use `python-dotenv` or `set_key` — credentials go through `deploy/vault.py` only.
- Don't use synchronous `httpx.Client` in async code — always `httpx.AsyncClient`.
