# Scratchpad

A per-session file-backed working area for the agent. Sits **outside** the LLM context window — the agent must explicitly read from it when it needs the contents.

> Source: `app/tools/scratchpad.py`, `app/toolsets/scratchpad.py`. Skill: `skills/scratchpad-skill/SKILL.md`.

---

## 1. Why scratchpads exist

LLM context is finite. If a tool returns 50KB of CSV, dumping that directly into the conversation history pushes useful turns out and bloats latency. The scratchpad pattern lets the agent:

1. Run a tool that produces a lot of raw output.
2. Persist that output to a scratchpad file (size cap enforced).
3. Hand only a **summary** back to the LLM.
4. When a downstream step needs the raw data, the agent reads the scratchpad on demand.

Used heavily by BigQuery, SP-API, Keepa bulk fetches, h10 analysis, and the spillover guardrail (`tool_output_spillover_guardrail`).

---

## 2. Storage

`tmp/scratchpads/{session_id}/{owner}__{name}.md` — Markdown files, one per scratchpad name + owning agent, scoped to the session.

`owner` is the agent that wrote the pad — auto-detected from `tool_context._invocation_context.agent.name`. Examples: `CoordinatorAgent`, `AmazonAgent`. Owner names are sanitized to `[a-zA-Z0-9_]` (spaces and dots collapse to underscores).

Backward compatibility: pads written before owner tagging (legacy bare `{name}.md`) remain readable, writeable, and deletable. Read order is owner-tagged first → legacy bare. Writes go to the owner-tagged path if owner is detected, else to the legacy bare path.

Name sanitization: any character outside `[a-zA-Z0-9_-]` is replaced with `_`. So `bigquery results.csv` becomes `bigquery_results_csv.md`.

Session_id resolution: `tool_context.session.session_id` (or `.id` if `session_id` is missing — older ADK API shape).

### Cross-agent reads

- Default `scratchpad_read(name)` finds the pad in priority order (this agent's owner, then legacy bare).
- Explicit `scratchpad_read(name, owner="AmazonAgent")` reads a pad owned by a specific sub-agent — useful when Coordinator wants to inspect what Amazon agent wrote.
- `scratchpad_list(owner="AmazonAgent")` filters the manifest to one agent's pads.

---

## 3. Tools

| Tool | Purpose |
|---|---|
| `scratchpad_write(name, content, tool_context)` | Append content to the named pad. Creates if missing. Returns `{"status": "success", "size_bytes": N}`. |
| `scratchpad_read(name, tool_context)` | Read full contents. If content is valid JSON, returns it parsed. Otherwise plain string. |
| `scratchpad_replace(name, content, tool_context)` | Overwrite (vs append). Used when re-running a step. |
| `scratchpad_clear(name, tool_context)` | Delete one pad. |
| `scratchpad_list(tool_context)` | List all pads in the current session (manifest). |
| `cleanup_session_scratchpads(session_id)` | Wipe the whole session dir. Called by `session_refresh` and on container exit. |

All exposed by `ScratchpadToolset` (`app/toolsets/scratchpad.py`).

---

## 4. Size caps

`scratchpad_write` enforces a per-pad cap (default 5 MB) and warns at 1 MB. Writes above the hard cap fail with `{"status": "error", "message": "Scratchpad would exceed N MB"}`.

The spillover guardrail (`tool_output_spillover_guardrail`, `app/callbacks/guardrails.py:677`) auto-spills oversized tool outputs into `_spillover__<tool>__<ts>` scratchpads, replacing the tool response the LLM sees with a short summary + a reference to where the data is. Read it explicitly with `scratchpad_read("_spillover__bigquery_query__1715...").`

---

## 5. Lifecycle

| Event | What happens |
|---|---|
| `scratchpad_write` first call | Session dir created on demand |
| `/reset session` | `cleanup_session_scratchpads(session_id)` wipes everything |
| Bot startup | `sweep_scratchpad_sessions()` removes session dirs untouched for `SCRATCHPAD_SESSION_TTL_DAYS` (default 7) |

The startup sweep is dir-level: each session dir's mtime is refreshed on every write inside it, so an actively-used session is never reaped. Only dead sessions (no writes for 7+ days) get removed.

Tune retention via the `SCRATCHPAD_SESSION_TTL_DAYS` vault key. Set to a large number to disable.

---

## 6. When to use scratchpad vs Neo4j vs plan result

| You want to keep... | Use |
|---|---|
| Raw tool output too big for the LLM context | Scratchpad (this session only) |
| A finding the user / future-session should remember | Neo4j memory (`create_record` via `knowledge-graph-skill`) |
| A short progress note for the next plan step | Plan `step.result` (≤1 paragraph) |
| A cross-session shared artifact between agents | Neo4j or a regular file under `data/` — scratchpad is intentionally ephemeral |

If you find yourself writing the same scratchpad on every session, that's a sign the content belongs in Neo4j, not the scratchpad.

---

## 7. Cross-agent sharing

Within a single session (e.g. Coordinator delegated to AmazonAgent), all sub-agents share the same `session_id` — so a scratchpad written by AmazonAgent is readable by Coordinator on the way back. No special mechanism needed.

Across A2A: scratchpads do not cross the wire. If ori-A wants to ship a large blob to ori-B, use the Phase 4 A2A `file_ref` mechanism (which itself uses a scratchpad as the staging area on the sender side).

---

## 8. Tests

| Test | Covers |
|---|---|
| `tests/test_scratchpad.py` | Write / read / replace / clear / list. Size cap. Session isolation. (Phase 7 extends with owner tagging.) |
| `tests/test_tool_output_spillover.py` | Spillover guardrail correctness — large tool responses get spilled, summary returned, scratchpad readable. |
