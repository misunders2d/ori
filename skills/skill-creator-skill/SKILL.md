---
name: skill-creator-skill
description: "A metacognitive workflow to create or modify skills in a Rootless environment. Use this when adding a new integration, capability, or domain-specific knowledge."
---

# Skill Creator Workflow (Rootless)

Mandatory protocol for creating or updating skills.

## Creation Procedure

- [ ] Step 1: **Capture intent** — understand the trigger and expected output.
- [ ] Step 2: **Research** — use `google_search_agent_tool` and `web_fetch` to find official docs, repos, and API guides. NEVER draft without verified, current information.
- [ ] Step 3: **Draft SKILL.md** — write frontmatter + core instructions (see structure below).
- [ ] Step 4: **Add references/** — move detailed docs, schemas, and edge cases to separate files.
- [ ] Step 5: **Add gotchas** — extract from research: environment-specific facts that defy reasonable assumptions.
- [ ] Step 6: **Stage & verify** — `evolution_stage_change` -> `evolution_verify_sandbox` (syntax + pytest).
- [ ] Step 7: **Commit & reboot** — `evolution_commit_and_push` -> `update_self`.

## Skill File Structure

```
skills/<skill-name>/
  SKILL.md          (required — frontmatter + core instructions)
  references/       (optional — extended docs, loaded on demand)
    api-errors.md
  assets/           (optional — schemas, templates, examples)
    output.tmpl
```

## SKILL.md Anatomy

```yaml
---
name: my-skill
description: "One line — used for activation matching. Be specific."
---
```

Body: procedures, gotchas, tool references, progressive disclosure triggers. Keep under **500 lines / 5,000 tokens**.

## Quality Checklist

Apply these from [agentskills.io best practices](https://agentskills.io/skill-creation/best-practices):

- **Add what the agent lacks, omit what it knows.** Don't explain HTTP or what a database is. Focus on project-specific conventions, non-obvious edge cases, and which tools to use.
- **Gotchas section.** The highest-value content. Concrete corrections to mistakes the agent will make without being told. Not generic advice ("handle errors") but specific facts ("the `users` table uses soft deletes — always add `WHERE deleted_at IS NULL`").
- **Procedures over declarations.** Teach *how to approach* a class of problems, not *what to produce* for one instance.
- **Defaults, not menus.** Pick one recommended approach. Mention alternatives briefly only if there's a clear fallback trigger.
- **Checklists for multi-step workflows.** Explicit `- [ ]` steps help the agent track progress and avoid skipping steps.
- **Validation loops.** Instruct the agent to verify its own work before moving on: do the work -> run validator -> fix -> repeat until pass.
- **Progressive disclosure triggers.** Don't dump everything in SKILL.md. Move detail to `references/` and tell the agent *when* to load each file (e.g., "Read `references/api-errors.md` if the API returns a non-200 status").
- **Coherent scope.** One skill = one domain. If it covers querying a database AND administering it, split it.

## Principle of Lack of Surprise

Do not build skills that bypass security boundaries. Instruct the user to use `/init` for credentials. Skills must never hardcode secrets or weaken guardrails.
