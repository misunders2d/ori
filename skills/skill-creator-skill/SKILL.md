---
name: skill-creator-skill
description: "A metacognitive workflow to create or modify skills in a Rootless environment. Use this when adding a new integration, capability, or domain-specific knowledge."
---

# Skill Creator Workflow (Rootless)

This skill defines the mandatory protocol for creating or updating skills.

## The Core Loop

1. **Capture Intent**: Understand the trigger and expected output.
2. **Research & Context**: 
   - Use the **external-research-skill** (specifically **google_search_agent_tool** and **web_fetch**) to find official documentation, GitHub repositories, or API guides. 
   - **NEVER proceed with drafting** until you have verified the current state of the art.
   - **Community Check**: Query `https://skills.sh/?q=[topic]`.
   - **ALWAYS include reference links** to official documentation, examples, and best practices.
3. **Draft with Progressive Disclosure**:
   - **Layer 1: Discovery (Metadata)**: Name and short description in YAML frontmatter.
   - **Layer 2: Activation (Instructions)**: Core workflow and entry points in `SKILL.md` (under 500 lines).
   - **Layer 3: Execution (Reference)**: Massive docs or edge cases in `references/`.
4. **Metacognitive Control**:
   - **Self-Assessment**: "Do I have the necessary schema to perform this accurately?"
   - **Gap Detection**: If unsure of an API, trigger a Layer 3 disclosure via `load_skill_resource`.
5. **ROOTLESS EXECUTION & TESTING**:
   - Use `evolution_stage_change` to write to sandbox.
   - Use `evolution_verify_sandbox` (syntax and full test suite).
   - Use `evolution_commit_and_push`.

## Anatomy of a Skill

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

## Self-Loading Skill Pattern (Metacognitive Prompting)
Instead of a static prompt, use a controller loop:
1. **Assess**: Identify relevant Skill IDs from metadata.
2. **Reflect**: Do you have the full instructions?
3. **Act**: If no, use `load_skill(skill_id)`.
4. **Execute**: Proceed only once instructions are loaded.

## Principle of Lack of Surprise
Do not build skills that bypass security boundaries. Instruct the user to use `/init` for credentials.
