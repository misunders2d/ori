---
name: external-research-skill
description: "Forces the agent to actively research official documentation, GitHub issues, and installed package versions before implementing new features OR debugging failures."
---

# External Research Mandate

Your pre-training knowledge has a cutoff date. Libraries change APIs, deprecate features, and introduce breaking changes. **You MUST NOT guess or hallucinate** implementations.

## The RRV Pattern (Search -> Reason -> Verify)

1. **Search**: Gather raw data from multiple sources.
2. **Reason**: Extract claims and formulate a draft implementation.
3. **Verify**: Red-team the answer — search for evidence that *disproves* your draft or indicates deprecation.

## Procedures

### New Features / Integrations
1. Identify the target framework or library.
2. `check_installed_package` — confirm what's actually installed locally.
3. `google_search_agent_tool` — locate official documentation.
4. `web_fetch` — fetch docs (converts HTML to markdown, ~90% token reduction).
5. Implement based on evidence, not memory.

### Bug Fixes / Failed Verifications
1. Confirm library version — the API may differ from what you expect.
2. Search GitHub Issues with the error message.
3. Search the web for migration guides or changelogs.
4. Fix with evidence. Cite the source.

## The One-Retry Rule

You get ONE attempt from your own knowledge. If verification fails, you **MUST** research externally before retrying. Every attempt after the first must be backed by external evidence.

Read `references/research-best-practices.md` for detailed guidance on source credibility scoring and RRV orchestration.

## Gotchas

- **`web_fetch` returns markdown, not HTML**: Don't try to parse HTML tags from its output. The conversion is automatic.
- **`llms.txt`**: If a target repo provides this file at the root, fetch it first — it's a structured documentation index purpose-built for LLMs. Saves tokens vs. crawling multiple pages.
- **`robots.txt` and `agents.txt`**: Respect them. If a site blocks agents, don't try workarounds.
- **Dynamic sites need Playwright**: If `web_fetch` returns empty/boilerplate content, the site is client-rendered. Note this and move on — don't burn retries.
- **Version mismatch is the #1 cause of failed fixes**: Always run `check_installed_package` before assuming an API exists. A function that works in v2.0 may not exist in v1.8.
- **Don't blind-copy Stack Overflow**: Adapt logic to Ori's internal architecture. External code uses different patterns, paths, and conventions.
