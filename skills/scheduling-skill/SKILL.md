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
- After scheduling, read `next_run_time` from the tool response — that is authoritative. Never narrate a different date.

## Channel routing — `deliver_to`

By default a scheduled task delivers back to the **same chat** it was scheduled from. To deliver elsewhere, pass `deliver_to` as a **session ID with platform prefix**:

| Prefix | Meaning | Example |
|--------|---------|---------|
| `sl_<channel_id>` | Slack channel or DM | `sl_C03A8FDLREH` |
| `tg_<chat_id>` | Telegram chat or group | `tg_-1001234567890` |

The Slack channel ID is the `<#CXXX\|name>` value (the `C…` part). For groups in Slack, ask the user or look it up — never invent IDs.

You can run **multiple recurring tasks delivering to different channels** in parallel. Each fire gets its own ephemeral session, so plans, scratchpads, and conversation history don't leak between jobs.

## Plan enforcement for scheduled tasks

If the scheduled task is non-trivial (3+ steps, must follow the same checklist every fire), bake the plan instruction into `task_prompt` itself:

```
CRITICAL: You MUST use create_plan with the following steps before doing anything else:
1. ...
2. ...
3. ...
Report tool failures immediately to the delivery channel.
```

This works because each fire runs the full CoordinatorAgent (with `plan_enforcer` callback wired in). The agent will create a fresh plan per fire — no leakage from previous runs.

## Posting to a channel without scheduling

If the user wants you to post to a Slack channel *right now* (no scheduling), use `slack_post_message(channel, text)` directly. Do NOT schedule a one-off "in 1 minute" as a workaround.

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
