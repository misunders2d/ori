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

## Auto-spillover (`_spill_*` pads)

When a tool returns an oversized response (default >8000 chars, configurable via `TOOL_OUTPUT_SPILL_THRESHOLD`), `ToolOutputSpilloverPlugin` automatically writes the full output to a scratchpad named `_spill_<tool>_<hash>` and replaces the tool result with:

```json
{
  "status": "spilled",
  "tool": "bigquery_query",
  "scratchpad_name": "_spill_bigquery_query_3f2a",
  "summary": "Tool returned 47230 chars (~11808 tokens). Call scratchpad_read('_spill_bigquery_query_3f2a') to load.",
  "size_chars": 47230,
  "preview": "<first 500 chars>"
}
```

**How to handle this in your reasoning:**
- The `preview` is usually enough for a smell test — confirm the tool succeeded and the data shape looks right.
- Only call `scratchpad_read(scratchpad_name)` when you actually need the full content (counting rows, picking specific records).
- For aggregations / summaries the user asked for, prefer narrowing the tool query first (smaller LIMIT, more selective WHERE) over reading the spilled pad — saves context.

This is enforced in the plugin layer; you don't trigger it manually. `_spill_*` pads are auto-cleaned at session end.

## Gotchas

- **Don't dump raw JSON** — extract the relevant fields before writing. A scratchpad full of raw API responses is as useless as no scratchpad.
- **Don't skip the read step** — the pad is outside your context. You must explicitly read it to use the data.
- **One pad per task** — don't mix unrelated research in the same pad.
- **Scratchpads are session-scoped** — they're cleaned up on `/reset` and on scheduled-task end. Don't use them for permanent storage; use `remember_info` for that.
