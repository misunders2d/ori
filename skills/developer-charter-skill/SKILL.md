---
name: developer-charter-skill
description: "Senior engineer charter — architecture style guide + process rules for the DeveloperAgent. Load this BEFORE drafting any code change, evolution plan, or refactor. Covers native-tools-first, async discipline, clean modules, security parity, research-before-retry, diagnose-first, import verification, and end-to-end testing."
---

# Developer Charter — TIER 2 (Architecture) + TIER 3 (Process)

DeveloperAgent inherits TIER 1 (security, availability, guardrails) inline because those are inviolable. TIER 2 (architecture style) and TIER 3 (process discipline) live here because they're reference material consulted once per plan, not security gates fired on every turn.

> Source-of-truth wider rules: `docs/AI_EDITS.md` (12 hard rules) and `docs/EVOLUTION.md` (workflow). This skill is the day-to-day cheat sheet.

---

## TIER 2 — Architecture

### NATIVE TOOLS FIRST

Prefer Python stdlib, ADK builtins, and existing utilities over external libraries. If a new dependency is genuinely necessary, justify it in the plan to the admin BEFORE staging. Adding `requests` when `httpx.AsyncClient` is already imported is a code-review fail. Adding `pandas` for a 4-row CSV scan is overkill.

### LEAST-PRIVILEGE LLM

Use deterministic code for parsing, I/O, validation. AI is for language only. If you find yourself prompting the LLM to extract a field from JSON, write the parser instead. If you find yourself prompting the LLM to validate a price is numeric, write the check. LLM calls cost money, are non-deterministic, and break under load.

### CLEAN MODULES

Tools in `app/tools/`, toolsets in `app/toolsets/`, agents in `app/sub_agents/`, callbacks in `app/callbacks/`. **No new top-level dirs without admin approval.**

Anti-pattern: wrapping a single function in a BaseToolset. A toolset's job is to group related tools; a one-tool toolset is just ceremony. Either add it to an existing relevant toolset, or expose it directly on the agent's `tools=[...]` list.

### ASYNC DISCIPLINE (CRITICAL — easy to miss)

This codebase is async (asyncio). **ALL I/O must use async APIs.**

- Never use `httpx.Client` (blocking) — always `httpx.AsyncClient`.
- Never use synchronous `open()` for network or long I/O in async contexts. Use `aiofiles` if the file path is hot; small config reads are fine sync.
- Never call `time.sleep()` in async code — use `await asyncio.sleep()`.
- Never run `subprocess.run` in async — use `asyncio.create_subprocess_exec`.
- Blocking I/O inside an `async def` blocks the entire event loop. Every other request hangs until it returns. Production hangs trace back to this every time.

If you must call a blocking library, wrap with `asyncio.to_thread(...)`.

### SECURITY PARITY

When adding a new interface, transport, or integration, **audit the existing sibling implementation** and carry over ALL its security measures. A new Telegram-style poller must include the same: secret scrubbing, access control, SSRF protection, secure key capture, file validation. A new interface with weaker security than the existing one is a regression.

Concrete: if Slack has a whitelist (`whitelist_chat`), Telegram must have it too. If A2A has `a2a_privacy_guardrail`, any new outbound transport needs an equivalent. Don't ship something less safe than what's already there.

---

## TIER 3 — Process

### RESEARCH BEFORE RETRY

One attempt from knowledge, then MUST research externally via `google_search` or `web_fetch` before trying again. Two consecutive blind retries on the same problem = you're guessing, and guessing burns admin trust and OpenRouter credits.

Especially relevant for: ADK 1.x version-specific API shapes (the docs at `google.github.io/adk-docs/` are authoritative — your pre-training knowledge is stale), Anthropic model deprecation, LiteLLM provider quirks, Gemini schema sanitisation. See `CLAUDE.md` "Google ADK is a moving target" section.

### DIAGNOSE FIRST

Read logs and code BEFORE forming hypotheses. Check `data/agent.log` for stack traces from the actual failing run. Don't theorize from the symptom in chat — pull the real trace.

For evolutions: read the files you're about to edit, not just the file you think is wrong. Cross-file failures (a tool wired without its parent's instruction update) only show up when you read both.

### VERIFY IMPORTS RESOLVE

After creating code that references new modules or files, confirm those files exist and the imports resolve. Run syntax checks on every new file (`uv run python -c "import ast; ast.parse(open(F).read())"` works fine).

A missing file masked by `try/except ImportError` is a silent failure, not a feature. The user thinks the feature shipped; production logs a warning and moves on.

### TEST THE FULL PATH

Before committing, verify the feature works end-to-end — not just that individual files parse. If you add an interface, confirm the poller starts. If you add a tool, confirm it's callable from the parent agent (not just declared). Partial implementations that silently fail are worse than no implementation.

`evolution_verify_sandbox` runs syntax + imports + pytest. None of that catches "the tool isn't on any agent's `tools=[]`" — that's on you. Cross-check via `grep -rn 'tool_name' app/sub_agents/`.

---

## Evolution catalog

- **BEFORE building**: `evolution_search` locally, then ask A2A friends via KnowledgeAgent. Don't reinvent.
- **AFTER committing**: `evolution_catalog` to save reusable evolutions for future-you.

---

## When the charter doesn't fit

If a legitimate change genuinely conflicts with one of these rules, say so explicitly and STOP. Surface the conflict to the admin. Don't paper over the gap with a workaround that subverts the rule (see `docs/AI_EDITS.md` "When the rules don't fit").
