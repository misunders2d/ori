---
name: scratchpad-skill
description: "Working memory protocol for multi-step tasks. Use the scratchpad to record intermediate findings without bloating LLM context."
---

# Scratchpad Protocol

You have access to a file-based scratchpad that lives OUTSIDE your conversation context. Use it for any task that involves multiple steps, partial results, or large intermediate data.

## When to Use

- **Multi-step research**: competitor analysis, multi-ASIN lookups, price comparisons
- **Large tool outputs**: Keepa product data, BigQuery results, web research findings
- **Iterative refinement**: collecting data across multiple tool calls before synthesizing

## Procedure

- [ ] Step 1: **Name your pad** — pick a short descriptive name (e.g., `competitor-research`, `price-analysis`)
- [ ] Step 2: **Write as you go** — after each tool call that produces useful data, `scratchpad_write(name, findings)` to record it
- [ ] Step 3: **Read when ready** — when you have enough data, `scratchpad_read(name)` to review everything at once
- [ ] Step 4: **Synthesize** — produce your final answer from the scratchpad contents
- [ ] Step 5: **Clean up** — `scratchpad_clear(name)` when the task is done

## Tools

| Tool | Purpose |
|------|---------|
| `scratchpad_write(name, content)` | Append findings to a named pad |
| `scratchpad_read(name)` | Read full pad contents |
| `scratchpad_replace(name, content)` | Overwrite with a condensed summary |
| `scratchpad_clear(name)` | Delete when done |
| `scratchpad_list()` | See all active pads |

## Gotchas

- **Don't dump raw JSON** — extract the relevant fields before writing. A scratchpad full of raw API responses is as useless as no scratchpad.
- **Don't skip the read step** — the pad is outside your context. You must explicitly read it to use the data.
- **One pad per task** — don't mix unrelated research in the same pad.
- **Scratchpads are session-scoped** — they're cleaned up on `/reset`. Don't use them for permanent storage; use `remember_info` for that.
