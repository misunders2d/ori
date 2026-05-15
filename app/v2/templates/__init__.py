"""V2 scheduler — templates package.

Phase 9 ships the first template (`OneOffReminder`) per
design §5.2 + ``docs/PHASE_9_PLAN.md`` §1.2.

Templates are thin facades over the typed-tool authoring
pipeline: a builder function produces a frozen
:class:`app.v2.models.schedule.ScheduleSpec` from a
narrowed argument surface, and the authoring tool wrapper
(`app/v2/authoring/templates.py` — slice 3) drives the
draft → dry_run → freeze → commit pipeline.

Phase 9 ships:
- :class:`OneOffReminderArgs` — typed Pydantic for the
  template's args payload (the reminder text).
- :func:`build_one_off_reminder` — pure-Python builder.

Future phases add ``RecurringSeriesFromSource`` /
``ChannelDigest`` (phase 11, after source loaders land).
"""

from app.v2.templates.one_off_reminder import (
    ONE_OFF_REMINDER_TEMPLATE_NAME,
    ONE_OFF_REMINDER_TEMPLATE_VERSION,
    OneOffReminderArgs,
    build_one_off_reminder,
)


__all__ = [
    "ONE_OFF_REMINDER_TEMPLATE_NAME",
    "ONE_OFF_REMINDER_TEMPLATE_VERSION",
    "OneOffReminderArgs",
    "build_one_off_reminder",
]
