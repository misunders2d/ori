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

### Schema extension (hard enforcement)

Each step grows two optional fields used by `plan_step_enforcer`:

```json
{
  "id": 0,
  "description": "Run BigQuery: revenue by category, Q2 2026",
  "status": "pending",
  "allowed_tools": ["bigquery_*", "scratchpad_write"],
  "must_call": [],
  "result": null
}
```

- `allowed_tools` — list of fnmatch glob patterns (`bigquery_*`, `keepa_*`, `*`). When non-empty, only tool names matching at least one pattern are permitted during this step. Empty list → unconstrained step (soft enforcement only, like the legacy behaviour).
- `must_call` — reserved for future use. Will track tools the agent must invoke before `complete_step` is allowed.

Pass via `step_constraints` argument to `create_plan` or `seed_plan`:

```python
seed_plan(
    session_id,
    task="Quarterly review",
    steps=["fetch sales", "summarize"],
    step_constraints=[
        {"allowed_tools": ["bigquery_*"]},
        {"allowed_tools": ["scratchpad_write"]},
    ],
)
```

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

### Hard (callback block)

`plan_step_enforcer` (`app/callbacks/guardrails.py`) layered on top of `plan_enforcer` — registered on Coordinator's `before_tool_callback`. It runs **first** so out-of-plan calls are rejected before admin/A2A guards do any work.

1. Looks up `get_current_step_constraints(session_id)`.
2. Pass-through cases: no plan, no in-progress step, empty `allowed_tools`.
3. For everything else, `fnmatch`-match `tool.name` against `allowed_tools`.
4. Mismatch → return `{"status": "error", "message": "Plan-step guardrail: tool `X` is not allowed for step Y (...). Allowed: [...]. Complete the current step (`complete_step`) or abandon the plan."}` — the agent sees this as a tool failure and adapts.

Always-exempt tools (allowed regardless of constraints) — without these the agent deadlocks:
- Plan lifecycle: `create_plan`, `get_next_step`, `complete_step`, `get_plan_status`, `abandon_plan`.
- Working memory: `scratchpad_read`, `scratchpad_write`, `scratchpad_list`, `scratchpad_replace`, `scratchpad_clear`.
- ADK primitive: `transfer_to_agent`.

Effect: the LLM cannot execute an off-plan tool while a constrained step is active. The model has to either complete the current step, abandon the plan, or work within the whitelist.

When `allowed_tools` is empty or absent on a step, enforcement falls back to the soft behaviour — useful for free-form discovery steps where the agent legitimately needs latitude.

### `complete_step` validation

`complete_step` rejects empty / whitespace-only `result` strings with `{"status": "error", "message": "result must be a non-empty string. ..."}`. Empty results were a silent-drift vector — the agent would mark "done" without explaining what it did, breaking auditability.

---

## 5. Scheduler re-entry

APScheduler fires a scheduled task → the run executes in its durable side-session (see §5.1) → seeds a plan (if the task carries `steps`) → runs the agent. The agent executes the step(s), calls `complete_step`, and emits a user-facing summary message.

Then the scheduler checks `plan_has_pending_steps(session_id)`:

- Yes → re-invoke the agent in the same session for the next step. The same plan is still in storage; `plan_enforcer` keeps injecting context.
- No → mark the run successful, post any final user-facing output to the configured channel (Slack/Telegram).

The loop is bounded by `MAX_PLAN_STEPS_PER_RUN` (default 20) to defend against accidentally-infinite plans.

### 5.1 v1fix Slice-1 — durable, reliable scheduled fire path

The **user** scheduled path (`schedule_one_off_task` / `schedule_recurring_task` → `app/tasks.py:run_scheduled_task`) no longer fabricates a throwaway ephemeral session per fire. (System maintenance tasks — `run_system_task` — are unchanged.) Slice-1 (reliability CORE only):

- **Durable side-session, linked to the chat.** The run uses session id `"<chat_session>::job::<job_id>"` under the chat's `user_id`, recovered at fire time from `notify.origin_session_id` (no new schedule-time plumbing). It is **get-or-create, never deleted** — so cross-fire state survives. It is deliberately *not* the live chat session, so a fire never collides with the user typing.
- **D5 (load-bearing).** The run goes through the **same boot Runner** (`run_bot.get_runner()`, `app=ori_app`), so the scheduled turn resolves under the **same ADK `app_name`** the live chat uses. A divergent `app_name` silently writes an orphan row and the in-chat follow-up breaks with no error — the acceptance test asserts the identity tuple and **fails loud** on divergence. (A per-fire `App` is *not* needed here; it arrives with the later per-job `SequentialAgent` slice, which **must** then add a runtime `app_name`-equality assertion.)
- **Logical-occurrence cursor.** `state["job:<id>:cursor"]` + `state["job:<id>:delivered_occurrences"]` live in the durable session state (written natively via `update_session_state`). After downtime every missed occurrence is computed from the job's trigger, delivered as **one consolidated message**, and recorded so a re-fire **never double-sends** (idempotent skip).
- **Delivery- AND agent-gated advance.** The cursor advance + delivered-marking happen **only when both** the channel accepted the message (`_deliver_with_fallback` now returns a bool) **and** the agent produced a genuine result (`agent_ok` — not raised, not empty, not a guardrail block, no terminal `AgentResponse.error`). A delivered *failure notice* is **not** a delivered occurrence. So a recoverable channel failure **or** a transient agent failure (LLM 503, tool timeout, rate/context limit) on a recurring job re-attempts that occurrence next wake; a one-off has no retry vehicle so it is logged-not-retried. At-least-once, never silently dropped (Rule 13, occurrence named in the log).
- **Per-job reliability kwargs.** The two user `add_job` sites pass `misfire_grace_time=None` (a late wake still fires — it was previously *dropped* past the global 1 h grace), `coalesce=True` (the cursor fans out the collapse), `max_instances=1` (single-writer guard for the cursor read-modify-write). The process-**global** default in `app/scheduler_instance.py` is **left untouched** — it also governs contract/system jobs and must not be flipped under them.
- **In-chat visibility.** The clean result is delivered to the channel and mirrored as one model event into the chat session (`_inject_into_session`, unchanged), so the user's next chat turn sees it.

**Out of Slice-1 (later, gated):** the per-job `SequentialAgent`/`LoopAgent` builder + universal job spec, per-step fidelity (QB), defer-blocks-downstream (QC), typed multi-target delivery + Sheets strict-append + forum-topic.

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
| `tests/test_scheduled_task_cleanup.py` | Durable side-session is never deleted; per-fire plan/scratchpad sidecar still reset |
| `tests/test_v1fix_scheduler_reliability.py` | Slice-1: D5 identity (fail-loud), durable linked side-session, never-double-send, outage catch-up consolidation, in-chat visibility, delivery/agent-failure ordering, per-job kwargs, global default unchanged |

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
