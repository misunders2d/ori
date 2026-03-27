---
name: skill-creator-skill
description: "A metacognitive workflow to create new skills, modify and improve existing skills, and measure skill performance. Use this when you are asked to evolve the agent by adding a new integration, capability, or domain-specific knowledge."
---

# Skill Creator Workflow

This skill defines the mandatory protocol for creating or updating other skills in the `skills/` directory. By following this metacognitive loop, you ensure that new capabilities are robust, tested, and structurally sound.

## The Core Loop

1. **Capture Intent**: Understand what the skill should do, when it triggers, and its expected output.
2. **Draft the Skill**: Create `skills/<skill-name>/SKILL.md` applying the Progressive Disclosure pattern (see below).
3. **Execute Test Cases**: Use your sandbox evolution tools to verify the technical logic. Run the tool/skills against dummy data or a local test script.
4. **Evaluate & Iterate**: Did the skill work as expected? If there are failures, update the `SKILL.md` or associated Python tools. Repeat.

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
- **Bundled Resources**: If a skill relies on massive documentation (like an entire API reference or cheatsheet), put that content in `references/<doc>.md` and instruct the reader inside `SKILL.md` to load it *only* when needed.

## Creating New Tools (Python)

If a skill requires new Python code (e.g., to ping an API):
- The executable Python tools go in `app/tools/`.
- Ensure tools are properly typed, return `dict`, and optionally accept `tool_context: ToolContext`.
- Provide a `dummy`/sandbox test script inside `./data/sandbox` to verify the syntax and imports before committing.

## Principle of Lack of Surprise
Do not build skills that bypass security boundaries. If an integration requires credentials, **do not read or write `.env` directly**. Instead, the skill must instruct you how to define the integration via `app/tools/integrations.py` which will securely prompt the human for credentials.
