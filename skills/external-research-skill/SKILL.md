---
name: external-research-skill
description: "Forces the agent to actively research official documentation, GitHub issues, and installed package versions before implementing new features OR debugging failures."
---

# External Research Mandate

Your pre-training knowledge has a cutoff date. Libraries change their APIs, deprecate features, and introduce breaking changes constantly. **You MUST NOT guess or hallucinate** implementations, fixes, or workarounds.

## The RRV Pattern (Search -> Reason -> Verify)
1. **Search**: Gather raw data from multiple sources.
2. **Reason**: Extract claims and formulate a draft implementation or answer.
3. **Verify**: Specifically "Red Team" the answer. Search for evidence that *disproves* your draft or indicates deprecation.

## Data Fetching & Token Optimization
- **Convert to Markdown**: Always use tools that strip HTML and convert pages to Markdown. This reduces token usage by ~90% and improves parsing accuracy.
- **Use `llms.txt`**: If a repository provides an `llms.txt` file at the root, use it! It's a machine-friendly index of documentation.
- **Source Credibility**: Prioritize `.gov`, `.edu`, and official vendor domains. Use a Cross-Encoder approach to select the most relevant chunks of data.

## When to Research

### 1. New Features / Integrations
1. **Identify the Target**: Understand what framework or library is needed.
2. **Check the Version**: Use `check_installed_package` to see what's actually installed locally.
3. **Search the Web**: Use `google_search_agent_tool` to locate the official documentation.
4. **Fetch Context**: Use `web_fetch` (with Markdown conversion).
5. **Etiquette**: Respect `robots.txt` and `agents.txt`. Identify as an AI agent via the `User-Agent` string.

### 2. Bug Fixes / Failed Verifications
1. **Check the Version**: Confirm the library version — the API may differ from what you expect.
2. **Search GitHub Issues**: Use the error message or symptom.
3. **Search the Web**: Find Stack Overflow answers, migration guides, or changelogs.
4. **Fix with Evidence**: Apply the fix based on evidence, not assumptions.

## The One-Retry Rule

You get ONE attempt based on your own knowledge. If that fails verification, you **MUST** research externally before your second attempt. Every retry after the first must be backed by external evidence.

## Best Practices
- **No Blind Copies**: Adapt logic to fit Ori's specific internal architecture.
- **Cite Sources**: Always include the Source URL and Access Date in your final report or code comments.
- **Handle Dynamic Content**: Use headless browsing (Playwright) only if a standard GET request fails.
