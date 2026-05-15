"""Production wiring for the phase-4 runtime injectables.

Phase 5 slice 1 per ``docs/PHASE_5_PLAN.md`` §3.1.

Every phase-4 runtime helper takes its clock and id factories
as required injectables — the reason being test determinism
(``datetime.now()`` and ``uuid.uuid4()`` make assertions
flaky). This module is the **only** runtime module allowed to
import ``uuid`` or call ``datetime.now()``. It exposes the
three production wirings that boot / binding callers pass in:

- :func:`prod_clock` — current UTC wall-clock time.
- :func:`prod_run_id_factory` — fresh UUID4 hex for a new
  ``runs.id``.
- :func:`prod_event_id_factory` — fresh UUID4 hex for a new
  ``events.id``.

A smoke test pins the inverse contract: this module DOES
import ``uuid`` and DOES call ``datetime.now``. Every OTHER
runtime module's smoke test already forbids them; if a
regression quietly moves the wall-clock / uuid calls back
into worker / wakeup / recovery / claim, both sides of the
guard fire together.

References:
- ``docs/PHASE_4_PLAN.md`` §6.2.1 (cross-cutting injection
  rule).
- ``docs/PHASE_5_PLAN.md`` §3.1.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def prod_clock() -> datetime:
    """Return the current wall-clock time in UTC.

    Always tz-aware. Phase-4 helpers (claim, recovery, worker,
    wakeup) require tz-aware datetimes; passing a naive
    ``datetime.now()`` would have raised ``NaiveDatetimeError``
    at the storage boundary. Using ``datetime.now(timezone.utc)``
    keeps the contract at the production wiring layer.
    """
    return datetime.now(timezone.utc)


def prod_run_id_factory() -> str:
    """Return a fresh UUID4 hex string suitable for a new
    ``runs.id``.

    32 lowercase hex chars, no dashes. The wakeup / recovery
    code paths assign this directly to ``Run.id`` /
    ``root_run_id`` (which carries the first-attempt self-
    reference invariant for new runs).
    """
    return uuid.uuid4().hex


def prod_event_id_factory() -> str:
    """Return a fresh UUID4 hex string suitable for a new
    ``events.id``.

    Same shape as :func:`prod_run_id_factory`. Kept as a
    separate callable because Run and Event id streams MAY
    diverge in a later phase (per-table monotonic counters,
    audit prefixes, etc.) — having distinct injection points
    means callers don't have to refactor to switch one stream.
    """
    return uuid.uuid4().hex


__all__ = [
    "prod_clock",
    "prod_event_id_factory",
    "prod_run_id_factory",
]
