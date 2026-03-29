---
name: external-research-skill
description: "Forces the agent to actively research official documentation, GitHub issues, and installed package versions before implementing new features OR debugging failures."
---

# External Research Mandate

Your pre-training knowledge has a cutoff date. Libraries change their APIs, deprecate features, and introduce breaking changes constantly. **You MUST NOT guess or hallucinate** implementations, fixes, or workarounds.

## When to Research

This mandate applies in TWO situations:

### 1. New Features / Integrations
When asked to implement a new framework, database connection, API library, or ADK feature you aren't 100% confident in:

1. **Identify the Target**: Understand what framework or library is needed.
2. **Check the Version**: Use `check_installed_package` to see what's actually installed locally.
3. **Search the Web**: Use `google_search_agent_tool` to locate the official documentation or GitHub repo.
4. **Fetch Context**: Use `web_fetch` to read the "Getting Started" pages, API reference, or README.
5. **Implement**: Only after reading official, current context should you begin coding.

### 2. Bug Fixes / Failed Verifications
When a verification check fails and you don't immediately recognize the root cause:

1. **Check the Version**: Use `check_installed_package` to confirm the library version — the API may differ from what you expect.
2. **Search GitHub Issues**: Use `search_github_issues` with the error message or symptom against the library's repo. Someone may have already reported or solved this.
3. **Search the Web**: Use `google_search_agent_tool` to find Stack Overflow answers, migration guides, or changelogs.
4. **Read the Source**: Use `web_fetch` on the most relevant GitHub issue or docs page.
5. **Fix with Evidence**: Apply the fix based on what you found, not on assumptions.

## The One-Retry Rule

You get ONE attempt based on your own knowledge. If that fails verification, you **MUST** research externally before your second attempt. Do not loop on the same approach hoping it will work. Every retry after the first must be backed by external evidence.
