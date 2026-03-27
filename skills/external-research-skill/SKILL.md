---
name: external-research-skill
description: "Forces the agent to actively research official documentation and GitHub repositories before implementing requested integrations or frameworks."
---

# External Research Mandate

When the user asks you to implement a completely new feature, framework, database connection (e.g. Pinecone), API library (e.g. Keepa), or even a new ADK feature that you aren't 100% confident in, you **MUST NOT GUESS OR HALLUCINATE** the implementation.

Instead:
1. **Identify the Target**: Understand what framework or library the user wants you to integrate.
2. **Search the Web**: Actively use the `google_search_agent_tool` to locate the official documentation, GitHub repositories, or official migration guides for that specific technology.
3. **Fetch Context**: Use the `web_fetch` tool to scrape and read the "Getting Started" pages, "Best Practices" guides, or README files from the target's repository.
4. **Implement**: Only after you have actively read the official context should you begin scaffolding the code utilizing `evolution_stage_change`.

Because APIs and libraries evolve constantly, assume your pre-training knowledge might be out of date. Always verify the current state of the art via live web research first!
