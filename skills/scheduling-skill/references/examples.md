# Scheduling Examples

Worked examples covering the patterns you'll hit most often. Read the one that matches the user's request before calling the tool.

---

## Example 1 — Recurring digest to a different Slack channel

**User (in their DM):** "Schedule a recurring news digest for the amazon-team channel. Every Mon/Wed/Fri at 12:30 Kyiv time. Search Amazon FBA news, verify against the professional knowledge base, post a summary."

**Channel ID lookup:** User says "amazon-team" — ask for or recall the ID. In Slack the mention `<#C03A8FDLREH|amazon-team>` carries the `C03A8FDLREH` part. Never invent it.

**Step 1 — confirm time:**
```
get_current_time(timezone="Europe/Kyiv")
→ {"datetime": "2026-04-13T12:21:14+03:00", "weekday": "Monday", ...}
```

**Step 2 — translate schedule:** Mon/Wed/Fri 12:30 = `30 12 * * MON,WED,FRI`. Use 3-letter names — numeric DOW is rejected because APScheduler's convention differs from Unix cron (0=Mon, not 1=Mon) and silently produces wrong days.

**Step 3 — verify "first run today":** now = 12:21, next slot today = 12:30 → today is fine. Otherwise, today would have been skipped.

**Step 4 — schedule:**
```python
schedule_recurring_task(
    task_prompt=(
        "CRITICAL: Use create_plan with these exact steps before doing anything else:\n"
        "1. Search the web for the last 48h of Amazon US FBA / seller news "
        "(policy changes, fee updates, regulations, notable seller reports).\n"
        "2. Summarize findings as concise bullet points.\n"
        "3. For each bullet, delegate to AmazonHeadAgent to check the 'professional' "
        "Pinecone namespace. If already recorded, prefix with '[Already in Knowledge Base]' "
        "and keep it to one line.\n"
        "4. Compose the final digest.\n"
        "5. Report any tool failures inline before posting.\n"
        "Deliver the digest to the destination channel."
    ),
    cron_expression="30 12 * * MON,WED,FRI",
    timezone="Europe/Kyiv",
    deliver_to="sl_C03A8FDLREH",
)
```

**Step 5 — quote the result back literally:**
> Scheduled `cron_4c2a6cc2`. Cron: `30 12 * * MON,WED,FRI` Europe/Kyiv. Next run: `2026-04-13 12:30:00+03:00`. Delivers to: amazon-team (sl_C03A8FDLREH).

**Don't say** "first run in 8 minutes" unless `next_run_time` literally shows today.

---

## Example 2 — Multiple recurring tasks, different channels

User wants three concurrent recurring jobs to different destinations:

| Job | Schedule | Channel |
|-----|----------|---------|
| News digest | Mon/Wed/Fri 12:30 | `sl_C03A8FDLREH` (amazon-team) |
| Daily ASIN check | Every day 09:00 | `sl_C04XXXXXX` (ops-room) |
| Weekly metrics | Monday 08:00 | `tg_-1001234567890` (Telegram group) |

Schedule them as three separate `schedule_recurring_task` calls with distinct `deliver_to` values. Each fire runs in its **own ephemeral session** (`sched_<task_id>`), so plans/scratchpads/history do not bleed between jobs. You can call them in parallel — no shared state to coordinate.

---

## Example 3 — One-off reminder, current chat

**User:** "Remind me tomorrow at 4pm to review the Q2 forecast."

```
get_current_time(timezone=user_tz)   # confirm today's date
schedule_one_off_task(
    task_prompt="Remind the user to review the Q2 forecast.",
    run_at_iso_datetime="2026-04-14T16:00:00",
    timezone="Europe/Kyiv",
    # deliver_to omitted → delivers to the current chat
)
```

---

## Example 4 — Post NOW vs. schedule

**User:** "Post the digest to amazon-team."

This is **not** a scheduling request. Do not call `schedule_one_off_task` with `run_at = now + 1min` as a workaround. Instead:

```
slack_post_message(channel="C03A8FDLREH", text="<digest body>")
```

`slack_post_message` exists for direct, immediate posting to any channel the bot is in.

---

## Example 5 — Editing without recreating

**User:** "Move the news digest to 13:00."

```
list_scheduled_tasks()
# find cron_4c2a6cc2

edit_scheduled_task(
    job_id="cron_4c2a6cc2",
    new_cron_expression="0 13 * * 1,3,5",
    timezone="Europe/Kyiv",
)
```

`edit_scheduled_task` preserves ownership, `deliver_to`, and the task prompt. Don't delete + recreate unless you also want to reset those.

---

## Example 6 — Cron DOW: names only

Numeric DOW is rejected by the tool. Use names.

| User says | Correct cron |
|-----------|--------------|
| "Every Monday 9am" | `0 9 * * MON` |
| "Weekdays 18:00" | `0 18 * * MON-FRI` |
| "Mon/Wed/Fri 12:30" | `30 12 * * MON,WED,FRI` |
| "Every Sunday midnight" | `0 0 * * SUN` |
| "Every 30 min" | `*/30 * * * *` |
| "First of month, 6am" | `0 6 1 * *` |

If you pass numeric DOW, the tool will return an error telling you to switch to names — heed it, don't retry with different numbers.

---

## Example 7 — `next_run` discipline

After any scheduling/list call, the response includes `next_run_time` (or rendered as `next_run`). That is the **only** date you may quote.

**Bad:**
> "Scheduled. First run: today in 8 minutes." *(when `next_run` actually shows tomorrow)*

**Good:**
> "Scheduled `cron_…`. `next_run`: 2026-04-13 12:30:00+03:00."

If the user says "but I wanted it today!", check `now` vs. the next slot — if `now` is past today's slot for *every* matching DOW, today truly is unreachable. In that case, the fix is usually a one-off run for today **plus** the recurring job, but only if the user asks for it. Don't auto-stack.

---

## Example 8 — "Did the job run?" → check the log

**User at 17:02:** "Did the 5 PM digest fire?"

Don't guess from `list_scheduled_tasks` (which shows only `next_run`, not history). Call the log:

```python
get_scheduled_task_logs(task_id="", limit=10)
# returns events (newest last). Look for the most recent fire_start/fire_end pair.
```

Interpret:
- `fire_start` + `fire_end` with `status=Completed` → it ran, quote `duration_ms` and `response_preview`.
- `fire_start` but no `fire_end` → still running (or crashed) — tell the user and re-check in a minute.
- `error` event → surface the error string verbatim. Don't sanitize.
- No events at all for this job → it hasn't fired yet. Cross-check `next_run_time` via `list_scheduled_tasks`.

Pass `task_id` to narrow to a single job — useful when multiple jobs fire close together.

## Example 9 — Plan enforcement that survives every fire

The CoordinatorAgent's `plan_enforcer` callback fires per-session. Because each scheduled fire runs in its own session, any plan you `create_plan` lives only for that fire — there's no carryover. To make every fire follow the same checklist, embed it in the `task_prompt`:

```
CRITICAL: Before any tool call, you MUST call create_plan with these steps:
1. <step>
2. <step>
3. <step>
Do not skip steps. Report failures immediately.
```

Why this works: when the scheduled session boots, your prompt is the very first user message. The model will create the plan; `plan_enforcer` then injects the active step into every subsequent model call until the plan is closed.
