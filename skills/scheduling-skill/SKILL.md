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

System (admin-only) variants: `schedule_system_task`, `schedule_recurring_system_task`, `run_system_task_now` — for maintenance chores, run with admin privileges.

## Cron format — read this carefully

`schedule_recurring_task` uses **standard Vixie cron** (5 fields): `minute hour day month day_of_week`.

- **Day of week: `0` or `7` = Sunday, `1` = Monday, … `6` = Saturday.**
- Example: `30 12 * * 1,3,5` = Mon/Wed/Fri at 12:30 (in the timezone you pass).
- The trigger is timezone-aware — pass an IANA tz like `Europe/Kyiv`.
- **First fire ≠ "today" automatically.** APScheduler computes the *next* matching slot strictly after `now`. If you schedule at 12:21 PM with `30 12 * * 1,3,5` and today is Monday, next fire = today 12:30. If you schedule at 12:35, next fire = Wednesday 12:30 — **today is skipped**. Don't promise "first run today" unless you've verified `now < next_slot_today`.
- After scheduling, read `next_run_time` from the response — that is authoritative. Never narrate a different date.

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
6. Cron DOW pitfalls table (right vs. wrong).
7. `next_run` discipline (don't fabricate dates).
8. Plan enforcement that survives every fire.

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
