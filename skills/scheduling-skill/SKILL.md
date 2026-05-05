---
name: scheduling-skill
description: "How to schedule one-off and recurring agent tasks, route their delivery to specific Slack/Telegram channels, and use plan enforcement reliably. Load this any time the user asks to remind, schedule, automate, or run something on a cron."
---

# Scheduling Protocol

Scheduled tasks run the agent (you) at a future time in an **isolated session per fire**, then deliver the result to a chosen channel. Use this skill whenever the user asks you to remind, schedule, automate, or run a task on a recurring cron.

## Tools

| Tool | Use |
|------|-----|
| `get_current_time(timezone)` | **ALWAYS call first.** Confirms current time + weekday in the user's tz. |
| `schedule_one_off_task(task_prompt, run_at_iso_datetime, timezone, deliver_to="")` | Run once at a specific ISO datetime. |
| `schedule_recurring_task(task_prompt, cron_expression, timezone, deliver_to="")` | Run on a 5-part cron schedule. |
| `list_scheduled_tasks()` | Show jobs the current user owns (admins see all). |
| `edit_scheduled_task(job_id, ...)` | Change prompt / time / cron without recreating. |
| `delete_scheduled_task(job_id)` | Cancel a job. |
| `get_scheduled_task_logs(task_id="", limit=50)` | Read the JSONL fire log. Use when the user asks "did it run?" / "what did it produce?" — **don't guess, check the log**. |

System (admin-only) variants: `schedule_system_task`, `schedule_recurring_system_task`, `run_system_task_now` — for maintenance chores, run with admin privileges.

## Cron format — read this carefully

`schedule_recurring_task` uses a 5-field cron: `minute hour day month day_of_week`.

**Day-of-week: ALWAYS use 3-letter names (`MON,TUE,WED,THU,FRI,SAT,SUN`). Never numeric.**

This codebase rejects numeric DOW at the tool level with an explicit error. The reason: APScheduler's `CronTrigger.from_crontab` uses `0=Mon, 6=Sun` (non-standard, contradicts Unix cron). Passing `1,3,5` silently produces Tue/Thu/Sat — a class of bug that's bitten this project before. Names are unambiguous.

- **Good:** `0 17 * * MON,WED,FRI` → Mon/Wed/Fri at 17:00
- **Good:** `0 8 * * MON-FRI` → weekdays at 08:00
- **Rejected:** `0 17 * * 1,3,5` → tool returns an error telling you to use names
- Minute/hour/day/month stay numeric as usual. Only DOW must be names.
- The trigger is timezone-aware — pass an IANA tz like `Europe/Kyiv`.
- **First fire ≠ "today" automatically.** APScheduler computes the *next* matching slot strictly after `now`. If you schedule at 12:21 PM with `30 12 * * MON,WED,FRI` and today is Monday, next fire = today 12:30. If you schedule at 12:35, next fire = Wednesday 12:30 — **today is skipped**. Don't promise "first run today" unless you've verified `now < next_slot_today`.
- After scheduling, read `next_run` from the tool response — that is authoritative. Never narrate a different date. The response also returns `now` (current time in the task's timezone) so you never have to guess "is today's slot still reachable?" — just compare `now` with the expected slot.

## Tool response shape

`schedule_recurring_task` returns:
```json
{
  "status": "success",
  "job_id": "cron_…",
  "cron": "30 12 * * MON,WED,FRI",
  "timezone": "Europe/Kyiv",
  "now": "2026-04-13T12:21:14+03:00",
  "next_run": "2026-04-13 12:30:00+03:00",
  "delivers_to": "sl_C03A8FDLREH",
  "message": "Scheduled cron_… now=… next_run=… delivers_to=…"
}
```
`schedule_one_off_task` returns the same shape (minus `cron`/`timezone`). **Report `next_run` exactly as given.** Do not translate, round, or describe in relative terms without the absolute timestamp.

## Reliability guarantees

Every fire goes through this flow — you can trust it:
1. `fire_start` logged.
2. Agent runs in ephemeral session `sched_<task_id>`.
3. Result (success **or failure**) is always delivered to the channel. Failures look like `:x: Scheduled task <id> failed.` — never fake-succeed silently.
4. If delivery to the target channel fails, a fallback delivery goes to the session that scheduled the job (`origin_session_id`), prefixed with a `:warning:` notice.
5. `fire_end` logged with `status` and `duration_ms`.

If a user reports "the job didn't run," the first move is always `get_scheduled_task_logs(task_id=…)`. Silence means the fire didn't happen — not that it failed quietly.

## Channel routing — `deliver_to`

By default a scheduled task delivers back to the **same chat** it was scheduled from. To deliver elsewhere, pass `deliver_to` as a **session ID with platform prefix**:

| Prefix | Meaning | Example |
|--------|---------|---------|
| `sl_<channel_id>` | Slack channel or DM | `sl_C03A8FDLREH` |
| `tg_<chat_id>` | Telegram chat or group | `tg_-1001234567890` |

The Slack channel ID is the `<#CXXX\|name>` value (the `C…` part). For groups in Slack, ask the user or look it up — never invent IDs.

You can run **multiple recurring tasks delivering to different channels** in parallel. Each fire gets its own ephemeral session, so plans, scratchpads, and conversation history don't leak between jobs.

## MANDATORY review-and-approve before EVERY scheduling tool call

Applies to **every** call of `schedule_one_off_task`, `schedule_recurring_task`, `schedule_system_task`, `schedule_recurring_system_task`, `run_system_task_now`, and `edit_scheduled_task`. No exceptions, regardless of whether `steps` is being used.

Why this is mandatory: when a scheduling request comes in, the agent often has the relevant context only as a *summary* of earlier turns (events compaction collapses raw turns after a threshold). Reconstructing `task_prompt` from memory recall or session summary is exactly how unrelated old jobs leak in (e.g. a stale MSRP task ending up as the body of an FBA-discrepancy schedule). The fix is a forced echo before the tool call.

**Required flow for every scheduling call:**

1. **Draft the exact tool call.** Build the `task_prompt` string from the **current conversation only** — what the user asked you to do in their most recent messages. Never use a `task_prompt` you got from `recall_*_memory`, `search_memory`, scratchpad, or the session summary as the source of truth. Memory is for context, not for authorship.
2. **Print the exact tool call for review** in a fenced code block, showing `task_prompt`, `cron_expression` / `run_at_iso_datetime`, `timezone`, `deliver_to`, and `steps` (if any) verbatim. No paraphrasing, no summarization, no "I will schedule a task that does X" prose substitute.
3. **Require explicit approval.** Wait for the user to confirm (`APPROVE`, `yes`, `proceed`, `да`, `так`, etc.). Any edit request → apply, re-print the full call, wait again. Approving a *description* in prose is NOT the same as approving the tool call body — the user must see the literal string the tool will receive.
4. **Call the tool unchanged.** Do not mutate the approved `task_prompt` between approval and tool call. If you find yourself reformatting, stop and re-confirm.

The approval step is the catch-net for both LLM "tidying" AND for the contamination case where the task_prompt was reconstructed from a memory hit. If you skip it, you will eventually schedule the wrong thing — there's a documented incident where this exact failure scheduled an MSRP task in place of an FBA discrepancy task.

## Enforced step-by-step scheduling

Use this pattern when the user says things like "must follow exactly", "precisely", "strict", "every step", "never skip", "daily checklist", or any framing that implies the task is a **playbook** — an ordered process that must execute the same way every fire, with no room for the agent to paraphrase, reorder, or skip.

**The mechanism (hard-wired, not LLM-decided):** pass a `steps: list[str]` kwarg to any scheduling tool. At fire time, the scheduler seeds the plan into storage **before the agent's first turn**, and `plan_enforcer` (wired on the coordinator as `before_model_callback`) injects the plan into every subsequent LLM turn. The agent cannot "forget" to plan — the plan already exists when it wakes up. This is enforced in code, not by instruction.

All scheduling tools accept the optional `steps` argument: `schedule_one_off_task`, `schedule_recurring_task`, `schedule_system_task`, `schedule_recurring_system_task`, and `run_system_task_now`.

### Collecting `steps` for enforced tasks

The general review-and-approve flow above already covers the approval gate. The only enforced-task-specific addition is the `steps` collection sources:

- User dictates them in chat → copy verbatim, NO SUMMARIZING!
- User points at a Google Sheet → call `sheets_read`, extract the step column. Keep the exact text.
- User points at a file → read it, extract the steps. Keep the exact text.

After collection, follow the same MANDATORY review-and-approve flow defined at the top of this skill — print the full tool call (now including `steps=[...]`) and wait for explicit approval before calling.

### Editing an enforced task

- To change the step list: `edit_scheduled_task(job_id, new_steps=[...])`. Follow the same review-and-approve flow before calling.
- To remove enforcement entirely: `edit_scheduled_task(job_id, clear_steps=True)` — the task reverts to normal LLM-decided flow.
- To add enforcement to an existing non-enforced task: `edit_scheduled_task(job_id, new_steps=[...])`.

### What NOT to do

- **Do NOT embed `CRITICAL: You MUST use create_plan…` style instructions in `task_prompt`.** That was the old pattern. It relied on the LLM obeying. The new pattern bypasses the LLM entirely for plan seeding — just use `steps`.
- **Do NOT call `create_plan` as the first action of an enforced task.** The plan already exists in storage. Go straight to `get_next_step` and execute.
- **Do NOT paraphrase step text.** If you think a step is unclear, ask the user to revise it before scheduling. Don't rewrite on their behalf.
- **DO NOT** rephrase, update, summarise or modify the task's steps or description without the user's EXPLICIT approval. Always present the suggested modifications before applyin.

### Where enforcement lives

- Schedule creation: `steps` is persisted as a top-level kwarg on the APScheduler job (alongside `task_prompt`, `notify`, `owner_user_id`).
- Fire time: `run_scheduled_task` / `run_system_task` calls `planner.seed_plan(session_id, task, steps)` **before** invoking the agent. No LLM round-trip; no opportunity to skip.
- Runtime: `plan_enforcer` reads from the planner store on every turn and injects into the prompt.

If `steps` is not provided, none of this triggers and the task runs as a normal scheduled agent invocation.

## Posting to a channel without scheduling

If the user wants you to post to a Slack channel *right now* (no scheduling), use `slack_post_message(channel, text)` directly. Do NOT schedule a one-off "in 1 minute" as a workaround.

## DM-ing a Telegram user by name

`telegram_send_dm(person, text)` resolves a name/username/handle against the local **roster** (auto-populated as users message the bot) and DMs them. Use when the user says "Message @Ruslan and tell him X" or "DM Hans this update".

**Hard Telegram constraint:** the bot can only DM users who have **previously messaged it at least once**. Slack accepts `@username` server-side; Telegram has no such API. If the tool returns `not_found`, ask the user to have the recipient send any message to the bot first, then retry. If it returns `ambiguous`, pick the right `user_id` from the candidate list and pass it as `person` to disambiguate.

## Worked examples

Read `references/examples.md` **before** writing your first scheduling tool call in a conversation. It covers:
1. Recurring digest delivered to a different Slack channel (full walkthrough).
2. Multiple parallel recurring jobs to different channels.
3. One-off reminder in the current chat.
4. Post-now (`slack_post_message`) vs. scheduling — which to pick.
5. Editing a job without recreate.
6. Cron DOW — names-only table.
7. `next_run` discipline (don't fabricate dates).
8. "Did it run?" → `get_scheduled_task_logs` workflow.
9. Plan enforcement that survives every fire.

If the user's request matches any of these patterns, copy the example shape — don't reinvent.

## Procedure

- [ ] Step 1: `get_current_time(user_tz)` to confirm now + weekday.
- [ ] Step 2: For recurring, translate the user's natural-language schedule to a cron expression. **Verify DOW numbering (1=Mon).**
- [ ] Step 3: Resolve the destination — current chat (omit `deliver_to`) or a specific channel (`sl_…` / `tg_…`).
- [ ] Step 4: Write `task_prompt` as a self-contained instruction to your future self. Include any plan-enforcement mandate.
- [ ] Step 5: Call the scheduling tool. Echo back the returned `job_id` and `next_run`.
- [ ] Step 6: If the user wants to verify, `list_scheduled_tasks` and confirm the same `next_run`.

## Gotchas

- **Don't fabricate `next_run`.** Always quote what the tool returned.
- **Don't post manually then also schedule** — pick one. Manual = `slack_post_message`. Recurring = scheduling tool.
- **Don't convert one-offs ↔ recurring via edit** — delete and recreate.
- **System tasks (`sys_*`)** are admin-only and write a `background_tasks` memory entry; ordinary scheduled tasks do not.
- **Owner scoping**: non-admin users only see/edit/delete their own jobs.

## Verifying a fire actually happened

When the user asks "did it run?" or "what did it produce?", **do not infer from memory or `next_run_time`** — call `get_scheduled_task_logs()`. The log contains one JSON line per event (`fire_start`, `fire_end`, `error`) with timestamps, duration, status, and response preview. This is the only reliable source of truth for execution history.

Typical shape of events:
```json
{"ts": "2026-04-13T17:00:02", "event": "fire_start", "task_id": "sched_xx", "kind": "scheduled", "prompt_preview": "...", "channel": "C03A..."}
{"ts": "2026-04-13T17:00:47", "event": "fire_end", "task_id": "sched_xx", "status": "Completed", "duration_ms": 45123, "response_preview": "..."}
```

If `fire_start` is present but no `fire_end`, the fire is still running or crashed mid-flight.
