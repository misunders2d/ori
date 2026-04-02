---
name: github-skill
description: "Operational rules and gotchas for GitHub interactions: authentication, commit signing, rate limits, and evolution push workflow."
---

# GitHub Operational Rules

## Authentication

- Use the `GITHUB_TOKEN` env var for all git operations. Never hardcode tokens.
- `evolution_commit_and_push` handles auth automatically via temporary clone with token URL.
- For the "Verified" badge: agent email must match `ID+app-name[bot]@users.noreply.github.com`.

## Evolution Push Procedure

1. Stage changes in sandbox via `evolution_stage_change`.
2. Verify with `evolution_verify_sandbox` (syntax + pytest).
3. Push with `evolution_commit_and_push` (requires admin token approval).
4. Reboot via `update_self` to apply.

All commits are signed with the `"evolved by {bot_name}"` trailer automatically.

## Gotchas

- **Conditional requests save rate limit**: Use ETags / `Last-Modified` headers. A `304 Not Modified` does NOT count against the rate limit.
- **Secondary rate limits on bulk writes**: Wait at least 1 second between mutative requests. A `403` or `429` with `retry-after` header means you're throttled — respect it.
- **Shallow clones**: `evolution_commit_and_push` uses `--depth 1` clones. You cannot access full history in the tmp repo. Use `git log` on PROJECT_ROOT instead.
- **Micro-commits waste history**: Group changes into semantic snapshots (one logical task = one commit). Don't commit file-by-file.
- **`.env` is gitignored**: Never push secrets. If you see `.env` in staged files, something is wrong.
- **`llms.txt`**: If a target repo provides this file, fetch it first — it's a machine-friendly documentation index.
- **`AGENTS.md`**: Check for this in external repos before contributing — it defines their rules of engagement.
