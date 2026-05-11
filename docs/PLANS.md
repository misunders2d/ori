# Plans

How Ori breaks complex tasks into ordered steps and keeps the agent on them.

> Source: `app/tools/planner.py`, `app/toolsets/planner.py`, `app/callbacks/guardrails.py:760` (`plan_enforcer`), `app/tools/scheduling.py:seed_plan` (re-entry pattern).

---

## 1. When to use a plan

A plan is right when the task:

- Has 2+ ordered steps that can't be done in a single tool call.
- Involves multiple tools (e.g. fetch BigQuery → generate chart → post to Slack).
- Is being delegated to a scheduled task that needs the agent to come back later for the next step.

A plan is wrong (overhead, drift risk) when the task is a single tool call or a Q&A round-trip.

---

## 2. Storage

`tmp/plans/{session_id}.json` — one file per session. JSON shape:

```json
{
  "task": "Pull this quarter's sales numbers and write a summary to Slack",
  "status": "active",          // active | completed | abandoned
  "created_at": 1715432100.5,
  "current_step": 1,
  "steps": [
    {
      "id": 0,
      "description": "Run BigQuery: revenue by category, Q2 2026",
      "status": "done",
      "result": "44 rows; full table at scratchpad/sales_q2.md"
    },
    {
      "id": 1,
      "description": "Summarize the top 5 categories with deltas vs Q1",
      "status": "in_progress",
      "result": null
    }
  ]
}
```

Plans are session-scoped — they go away when the session is reset. The scheduler uses fresh session IDs for fired tasks, so there's no collision between a user's interactive session and a background job.

### Phase 3 schema extension (hard enforcement)

After Phase 3 lands, each step grows two optional fields used by `plan_step_enforcer`:

```json
{
  "id": 0,
  "description": "Run BigQuery: revenue by category, Q2 2026",
  "status": "pending",
  "allowed_tools": ["bigquery_*", "scratchpad_*"],
  "must_call": ["mark_step_done"],
  "result": null
}
```

- `allowed_tools` — glob patterns (`bigquery_*`, `keepa_*`, `*`); the agent can only call tools matching one of them while this step is active.
- `must_call` — tool the agent must invoke before advancing.

If both fields are omitted, the step is unconstrained (soft enforcement only, like today).

---

## 3. Lifecycle

### Creation (interactive)

`create_plan(task_description, steps[], tool_context)` — agent calls this when it decides a plan is warranted. Refuses if there's already an active plan for the session (the agent must `abandon_plan` first).

### Seeding (scheduler)

`seed_plan(session_id, task, steps[])` — writes the plan **before the agent's first turn**, no LLM involved. The scheduler does this when a recurring/one-off job carries an enforced step list. By the time `plan_enforcer` runs on the first model call, the plan is already in place — the agent never has the opportunity to skip the plan-creation step.

### Step advancement

| Tool | What it does |
|---|---|
| `get_next_step(tool_context)` | Returns the current pending step. Marks it `in_progress`. |
| `complete_step(tool_context, result)` | Marks the current step `done`, advances `current_step`. `result` is a short string the next step can use. |
| `get_plan_status(tool_context)` | Whole plan, all steps + statuses. |
| `abandon_plan(tool_context)` | Marks plan `abandoned`. Releases the session for new plans. |

### Completion

When the last step is marked done, `status` flips to `completed`. The file stays around until the session is reset (so the user can inspect it).

---

## 4. Enforcement

### Today (soft, prompt-injection)

`plan_enforcer` (`app/callbacks/guardrails.py:760`) runs on every `before_model_callback`:

1. Looks up the active plan via `get_active_plan_context(session_id)`.
2. If one exists, appends a summary to `llm_request.contents`: "You are on step N of M: <description>. Tools available for this step: ... Call `complete_step` when done."
3. Returns `None` — pass-through.

The LLM **can** still call any tool it wants and ignore the injected context. It's a soft fence. In practice the well-trained models follow it; jailbroken or off-script ones don't.

### Phase 3 (hard, callback block)

A new `plan_step_enforcer` callback layered on top of `plan_enforcer`:

1. Same lookup.
2. For each tool call in `llm_request`, check `tool_name` against `current_step.allowed_tools` glob list.
3. Mismatch → return a synthetic `LlmResponse` that says: "Tool `<name>` is not allowed for step `<id>`. Allowed: `[...]`. Either complete the current step (`mark_step_done`) or abandon the plan."

Effect: the LLM physically cannot execute an off-plan tool while the plan is active. The model has to either continue the plan, call the step-completion tool, or abandon.

When `allowed_tools` is empty or absent on a step, enforcement falls back to today's soft behaviour — useful for free-form discovery steps where the agent legitimately needs latitude.

---

## 5. Scheduler re-entry

APScheduler fires a scheduled task → creates a fresh session → seeds a plan (if the task carries `steps`) → runs the agent for one turn. The agent executes one step, calls `complete_step`, and emits a user-facing summary message.

Then the scheduler checks `plan_has_pending_steps(session_id)`:

- Yes → re-invoke the agent in the same session for the next step. The same plan is still in storage; `plan_enforcer` keeps injecting context.
- No → mark the run successful, post any final user-facing output to the configured channel (Slack/Telegram), close the session.

The loop is bounded by `MAX_PLAN_STEPS_PER_RUN` (default 20) to defend against accidentally-infinite plans.

---

## 6. Authoring good plans

Rules of thumb:

- **Atomic steps.** "Fetch the data" is fine; "fetch, analyze, write" should be three steps.
- **Concrete tools per step (Phase 3+).** If a step is "use Keepa to look up the ASIN", set `allowed_tools: ["keepa_*", "scratchpad_*"]`. Avoid `["*"]` unless the step is genuinely discovery.
- **One `must_call` per step (Phase 3+).** Usually `complete_step` or `mark_step_done`.
- **Result strings are short.** `result` is shown to the LLM at the next step — keep it under a paragraph, use scratchpads for anything bigger.

Bad pattern: a single 30-step plan where every step is `allowed_tools: ["*"]`. That's a plan in name only.

---

## 7. Tests

| Test | Covers |
|---|---|
| `tests/test_planner.py` | Plan create / advance / complete / abandon |
| `tests/test_plan_enforcer.py` (Phase 3, new) | Hard-block: tool outside `allowed_tools` returns refusal; `mark_step_done` advances; absence of plan = pass-through |
| `tests/test_scheduled_task_cleanup.py` | Scheduler re-entry doesn't leak plans across sessions |

---

## 8. Tooling

| Tool | Purpose |
|---|---|
| `create_plan` | Agent-side plan creation |
| `seed_plan` | Programmatic plan seeding (no LLM) |
| `get_next_step` | Read the current pending step |
| `complete_step` | Mark current step done, advance |
| `get_plan_status` | Whole-plan view |
| `abandon_plan` | Cancel active plan |
| `plan_has_pending_steps` | Helper for scheduler re-entry decision |
| `get_active_plan_context` | Helper for `plan_enforcer` injection |
