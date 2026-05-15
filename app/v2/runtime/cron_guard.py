"""Shared cron-expression guards for the v2 runtime.

Phase 5 slice 1 per ``docs/PHASE_5_PLAN.md`` §3.2.3.

Two callers share these guards: ``app/v2/runtime/wakeup.py``
(checks at fire time) and ``app/v2/runtime/binding.py``
(checks at APScheduler registration time, before the cron
expression reaches APScheduler's parser). Originally lived
private inside ``wakeup.py`` as ``_reject_numeric_dow`` in
the phase-4 slice-5b commit; extracted to this module in
phase 5 so the binding can fail fast at register time
rather than waiting until first fire.

Reviewer round-1 finding: relying on APScheduler to reject
numeric day-of-week is invalid. APScheduler ACCEPTS numeric
DOW with Monday=0 semantics, which silently diverges from
standard Unix cron's Sunday=0. The chokepoint that catches
this MUST live in v2 — before APScheduler ever sees the
expression.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.2 (Trigger union).
- ``docs/PHASE_4_PLAN.md`` §6.2 (slice 5b — original wakeup
  numeric-DOW reject).
- ``docs/PHASE_5_PLAN.md`` §3.2.3 (extraction rationale).
"""

from __future__ import annotations


def reject_numeric_dow(cron_expr: str) -> None:
    """Reject numeric day-of-week fields in v2 cron expressions.

    Standard Unix cron uses ``Sunday=0`` while APScheduler's
    ``CronTrigger`` uses ``Monday=0``. The numeric forms parse
    in both — they just mean different days — so an author who
    types ``0 18 * * 1-5`` expecting "Mon-Fri Unix-style" would
    silently get "Tue-Sat APScheduler-style". To eliminate the
    ambiguity, v2 cron triggers MUST express the DOW field as
    ``*`` or named days (``MON``, ``TUE``, ...) with range or
    list syntax (``MON-FRI``, ``MON,WED,FRI``). Any digit in
    the DOW field is rejected at the v2 layer before
    APScheduler ever sees the expression.

    Step syntax (``MON-FRI/2``, ``*/2``) is rejected too —
    the ``/N`` step always contains a digit and the choice of
    rejecting "any digit anywhere in DOW" keeps the rule
    simple and unambiguous. Authors that need bi-weekly DOW
    semantics can express it via the day-of-month field or
    the schedule's caller logic.

    ``?`` is NOT in the allow-list because APScheduler's
    ``CronTrigger`` refuses it for ``day_of_week`` (probed
    against APScheduler 3.11.x: ``Unrecognized expression
    "?" for field "day_of_week"``); listing it here would
    only confuse authors who try it and hit a parser error
    one layer down.

    Note: this rule applies to the DOW (5th) field only — the
    other four fields (minute / hour / day-of-month / month)
    use unambiguous numeric semantics and stay unrestricted.

    Raises ``ValueError`` with a message that names the
    rejected field. The phase-1 ``CronTrigger`` model already
    validates the 5-field count, so this helper assumes a
    well-shaped 5-field input; the defensive recheck is cheap
    and surfaces shape problems too.
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        # Defensive — phase-1 CronTrigger model rejects this
        # earlier, but a hand-built trigger that bypasses
        # Pydantic could land here.
        raise ValueError(
            f"cron expression must have exactly 5 whitespace-"
            f"separated fields; got {len(fields)} in "
            f"{cron_expr!r}."
        )
    dow = fields[4]
    if any(c.isdigit() for c in dow):
        raise ValueError(
            "numeric day-of-week is rejected in v2 cron "
            f"expressions (got {dow!r} in {cron_expr!r}). "
            "APScheduler interprets numeric DOW as Monday=0 "
            "while standard Unix cron uses Sunday=0 — to "
            "avoid the ambiguity, v2 cron triggers MUST use "
            "'*' or named days (MON, TUE, ...) with range "
            "(MON-FRI) or list (MON,WED,FRI) syntax. No "
            "numeric components anywhere in the DOW field — "
            "step syntax ('MON-FRI/2', '*/2') is rejected "
            "for the same reason."
        )


__all__ = ["reject_numeric_dow"]
