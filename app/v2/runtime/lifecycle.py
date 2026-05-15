"""Schedule-status lifecycle hooks for the v2 runtime.

Phase 5 slice 6 per ``docs/PHASE_5_PLAN.md`` section 3.4.

Four pure-function hooks the future authoring tools call
when a schedule's status flips:

- :func:`on_schedule_paused` -- a schedule's status went
  ``active -> paused``. Removes the APScheduler job;
  pending Run rows stay in the DB but cannot fire (claim's
  schedule-status predicate refuses them, and the binding
  no longer dispatches the wakeup).
- :func:`on_schedule_archived` -- ``active -> archived``.
  Same shape as paused for phase 5; explicit cancellation
  of existing pending runs is the
  ``paused_pending_policy`` open question (PHASE_4_PLAN
  section 12 item 5) and lands when that policy ships.
- :func:`on_schedule_resumed` -- ``paused -> active``.
  Re-registers the schedule. Caller passes the fresh spec
  (status flipped back to active) so register sees the
  post-resume trigger config.
- :func:`on_schedule_revised` -- the schedule's trigger /
  delivery / failure-policy was edited and a new revision
  was authored. The binding swaps the trigger via
  ``reregister`` (atomic ``reschedule_job``).

Phase 5 SHIPS the hooks + their tests; phase 7 wires them
into the storage helpers. Until then, callers (test rigs,
dev scripts) invoke them by hand. The hooks are pure
functions over a binding -- they do NOT touch the v2 DB
themselves (the storage update happens in the authoring
tool that calls the hook).

References:
- ``docs/PHASE_5_PLAN.md`` section 3.4 + section 5.4.
- ``docs/PHASE_4_PLAN.md`` section 12 item 5
  (paused_pending_policy).
"""

from __future__ import annotations

from app.v2.models.schedule import ScheduleSpec
from app.v2.runtime.binding import SchedulerBinding


def on_schedule_paused(
    binding: SchedulerBinding,
    schedule_id: str,
) -> None:
    """A schedule moved from ``active`` to ``paused``.

    Removes the APScheduler job so the binding stops
    dispatching the wakeup for this schedule. Pending Run
    rows already in the DB stay -- claim's
    schedule-status predicate refuses to promote them
    while the schedule is paused.
    """
    binding.unregister(schedule_id)


def on_schedule_archived(
    binding: SchedulerBinding,
    schedule_id: str,
) -> None:
    """A schedule moved from ``active`` to ``archived``.

    Same APScheduler-side handling as paused: remove the
    job, leave pending Run rows alone. Explicit
    cancellation of those rows lands when
    ``paused_pending_policy`` (PHASE_4_PLAN section 12
    item 5) ships -- the authoring layer that owns the
    archive operation will then update each pending Run
    row alongside this hook call.
    """
    binding.unregister(schedule_id)


def on_schedule_resumed(
    binding: SchedulerBinding,
    spec: ScheduleSpec,
) -> None:
    """A schedule moved from ``paused`` (or ``archived``)
    back to ``active``.

    Re-registers via the binding. ``spec`` MUST be the
    fresh spec with ``status=ACTIVE`` so register sees the
    post-resume trigger config -- the binding does NOT
    re-read the DB. Caller is the authoring tool that
    flipped the status; passing the spec it just wrote is
    its responsibility.
    """
    binding.register(spec)


def on_schedule_revised(
    binding: SchedulerBinding,
    spec: ScheduleSpec,
) -> None:
    """A schedule's trigger / delivery / failure-policy was
    edited and a new revision was authored.

    Calls ``binding.reregister(spec)`` -- atomic trigger
    swap via APScheduler's ``reschedule_job``. The new
    spec keeps the same ``schedule_id``; ``spec.hash``
    bumps to the new revision. If the new trigger is
    OneOff and the v2 ``runs`` table already has a row
    for this schedule_id, ``reregister`` raises
    ``ValueError`` -- callers should create a NEW
    schedule_id for the revised intent in that case.
    """
    binding.reregister(spec)


__all__ = [
    "on_schedule_archived",
    "on_schedule_paused",
    "on_schedule_resumed",
    "on_schedule_revised",
]
