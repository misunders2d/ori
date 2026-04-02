---
name: skill-creator-skill
description: A metacognitive workflow to create or modify skills in a Rootless environment. Use this when adding a new integration, capability, or domain-specific knowledge.
---

# Skill Creator Workflow (Rootless)

This skill defines the mandatory protocol for creating or updating other skills in the `skills/` directory. By following this metacognitive loop, you ensure that new capabilities are robust, tested, and structurally sound.

## The Core Loop

1. **Capture Intent**: Understand what the skill should do, when it triggers, and its expected output.
2. **Research & Context**: 
   - Use the `external-research-skill` (specifically `google_search_agent_tool` and `web_fetch`) to find official documentation, GitHub repositories, or API guides for the target module or integration. **NEVER proceed with drafting** until you have verified the current state of the art.
   - When creating or updating skills, you MUST first query `https://skills.sh/?q=[topic]` (e.g., a2a) for community examples.
   - **NO BLIND COPIES**: You must adapt the logic to fit Ori's specific internal architecture, tools, and constraints.
   - **ALWAYS include reference links** to official documentation pages, examples, best practices, if available, in the skill text. The skill must not be stale and the agent must be able to refine the skill if the source documentation introduces changes.
3. **Draft the Skill**: Design the skill structure applying the Progressive Disclosure pattern.
4. **ROOTLESS EXECUTION & TESTING**:
   - **You CANNOT write directly to `skills/`** on the local host as it is Read-Only.
   - Use `evolution_stage_change` to write the `SKILL.md` and any associated tools into the writeable `data/sandbox/` directory.
   - Use `evolution_verify_sandbox` to verify the technical logic and syntax within that sandbox.
   - Use `evolution_commit_and_push` to send the new/updated skill to the GitHub repository.
5. **Evaluate & Reboot**: Once pushed, ask the Coordinator to run `update_self` to pull the new skill onto the local host and activate it.

## Anatomy of a Skill

Every skill MUST follow this folder structure:

```
skills/<skill-name>/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── references/ (optional, for extensive documentation)
    └── docs.md
└── scripts/ (optional, for reusable executable python tools)
    └── helper.py
```

### Progressive Disclosure
- **Metadata**: Put `name` and `description` in YAML frontmatter. The description acts as the *trigger* for the skill. Make it punchy.
- **SKILL.md Body**: Keep this file under 500 lines. Use it for the core workflow, entry points, and "when to use what".
- **Bundled Resources**: If a skill relies on massive documentation, put that content in `references/<doc>.md` and instruct the reader inside `SKILL.md` to load it *only* when needed.

## Creating New Tools (Python)

If a skill requires new Python code (e.g., to ping an API):
- The executable Python tools go in `app/tools/`.
- Ensure tools are properly typed, return `dict`, and optionally accept `tool_context: ToolContext`.
- **Sandbox Requirement**: You MUST provide a dummy/test script inside the sandbox to verify imports and logic before pushing to Remote.

## Principle of Lack of Surprise
Do not build skills that bypass security boundaries. If an integration requires credentials, **do not write to .env directly**. The skill must instruct the user to use the `/init` command or the secure key capture mechanism which will securely prompt the human for credentials.
