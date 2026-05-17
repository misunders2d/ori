"""V2 scheduler — failure-monitor PURE detector.

Phase 15 slice 2 per ``docs/PHASE_15_PLAN.md`` §1/§4/§9 +
the claude-reviewer round-1 disposition (Q1/Q2) + slice-2
pre-arm. Build-the-layer, NOT wired.

Realises the design §9:1329 background failure-monitor
QUERY as a PURE detector — and ONLY the query:

    SELECT admin_alert_sent rows whose id is NOT among any
    admin_alert_acked.correlates AND whose ts < now - threshold

Honest scope (the slice-1 / phase-11-ChannelDigest-(b)
discipline): §9:1329 is an inherently GLOBAL cross-schedule
sweep ("every N minutes … Re-alert"). The shipped read
surface (``list_events_for_run`` / ``list_events_for_schedule``
/ ``get_last_emit_succeeded``) is per-run / per-schedule —
NONE composes a global cross-schedule admin-alert query, and
slice-2 may NOT add a storage read (the §11.1 byte-proof
binding). Per-schedule iteration would silently MISS unacked
alerts on paused / archived schedules (a correctness bug +
honest-scope debt). So the detector issues the canonical
§9:1329 query directly as a PURE read (SELECT only, ZERO
mutation — PURITY-pinned). This is the detector's own
canonical query, NOT a re-implementation of any shipped
per-schedule helper (none exists for the global sweep — the
conductor pre-arm "NO SQL reimpl IF a shipped read composes
it" conditional explicitly anticipates this).

NOT wired (Q1/Q2 build-the-layer): NO periodic / APScheduler
binding, NO re-alert dispatch side-effect — DEFERRED,
closeout-recorded. Reuses ``admin_alert_sent`` /
``admin_alert_acked`` (both already in the v001 EventKind
CHECK) — no new EventKind, no v002. A future re-alert
needing otherwise = a verify-first fork.

``now`` is an injected caller clock (DI) — this module
imports no clock (the phase-5 hard rule 10 carried 11–15).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §9 (the §9:1329 monitor)
- ``docs/PHASE_15_PLAN.md`` §1 / §4 / §9
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app.v2.enums import EventKind
from app.v2.observability.results import OverdueAlert
from app.v2.storage.connection import assert_connection_ready

_SENT = EventKind.ADMIN_ALERT_SENT.value
_ACKED = EventKind.ADMIN_ALERT_ACKED.value


def failure_monitor_scan(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    threshold: timedelta,
) -> list[OverdueAlert]:
    """Return every ``admin_alert_sent`` that is BOTH unacked
    (no ``admin_alert_acked`` correlates its id) AND overdue
    (``sent_ts < now - threshold``), oldest-first.

    PURE: a single read-only SELECT, ZERO mutation, ZERO
    event write, ZERO DM / dispatch. ``assert_connection_ready``
    fails loud on an unprepared connection (same guard the
    shipped storage reads use) — it does not mutate.
    """
    assert_connection_ready(conn)
    cutoff = now - threshold

    rows = conn.execute(
        "SELECT id, schedule_id, run_id, ts, kind, correlates "
        "FROM events WHERE kind IN (?, ?)",
        (_SENT, _ACKED),
    ).fetchall()

    # The set of admin_alert_sent ids that have been acked.
    acked_ids = {
        correlates
        for (_id, _sch, _run, _ts, kind, correlates) in rows
        if kind == _ACKED and correlates is not None
    }

    overdue: list[OverdueAlert] = []
    for (eid, sch, run, ts, kind, _correlates) in rows:
        if kind != _SENT:
            continue
        if eid in acked_ids:
            continue  # ack clears it (correlation match)
        sent_ts = datetime.fromisoformat(ts)
        if sent_ts >= cutoff:
            continue  # below threshold — not overdue
        overdue.append(
            OverdueAlert(
                alert_event_id=eid,
                schedule_id=sch,
                run_id=run,
                sent_ts=sent_ts,
                age_seconds=int((now - sent_ts).total_seconds()),
            )
        )

    overdue.sort(key=lambda a: a.sent_ts)
    return overdue


__all__ = ["failure_monitor_scan"]
