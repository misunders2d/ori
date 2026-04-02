---
name: github-skill
description: "A skill providing guidelines for interacting with GitHub repositories, code execution, and Git logic."
---

# GitHub Skill

This skill defines the operational logic for interacting with GitHub.

## 1. Identity & Authentication
- **Prefer GitHub Apps**: Use GitHub App installation tokens instead of Personal Access Tokens (PATs) for dedicated identity and higher rate limits.
- **Verified Badge**: For the "Verified" badge to appear, the agent's email must match the bot ID: `ID+app-name[bot]@users.noreply.github.com`.
- **Commit Signing**: All git commits MUST be signed. The `evolution_commit_and_push` tool handles this automatically with the "evolved by {bot_name}" signature.

## 2. Rate Limit Management
- **Efficiency**: Use **Conditional Requests** (ETags and `Last-Modified` headers). If GitHub returns `304 Not Modified`, it does NOT count against the rate limit.
- **Throttling**: For bulk edits, wait at least 1 second between mutative requests to avoid secondary rate limits.
- **Retry Logic**: If you receive a `403` or `429` error, check the `retry-after` header.

## 3. Agent-Repo Standards
- **`llms.txt`**: Fetch this file if available for a structured index of repository documentation.
- **`AGENTS.md`**: Check for this file to understand the "rules of engagement" (e.g., "Always run tests before opening a PR").

## 4. Operational Logic
- Use `GITHUB_TOKEN` to securely push to `GITHUB_REPO`.
- When the agent needs to evolve itself, push changes back to the remote repository and signal for an update.
- **Micro-Commits**: Avoid flooding the history. Group changes into **semantic snapshots** (logical task completions).

## Best Practices
- **No Direct `.env` Updates**: Never push secrets to the repository.
- **Audit Trail**: Ensure every AI-generated change is clearly distinguished from human code via the `[bot]` suffix.
