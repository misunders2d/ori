"""Boot recovery scan + stale-row remediation for the v2 runtime.

Phase 4 slice 3 per ``docs/PHASE_4_PLAN.md`` §5.

The scan runs at boot (before workers start polling) and walks
the ``runs`` table for rows the worker can no longer own:

- ``status='claimed'`` rows whose ``claimed_at`` is older than
  ``claimed_timeout`` — a worker died after the claim UPDATE
  but before transitioning the row to ``running``. The body
  never ran; no external work was performed.
- ``status='running'`` rows whose ``started_at`` is older than
  ``running_timeout`` — a worker died mid-execution; emit
  side-effects MAY have partially fired (phase 4's empty worker
  body neutralises this until later phases wire real emit; the
  retry path then relies on the ledger's idempotency dedup).
- ``status='running'`` rows with ``started_at IS NULL`` — a
  defensive case for an invariant violation; treated as
  immediately stale rather than silently never-recovering.

Both prior statuses apply ``RecoveryPolicy.QUEUE_RETRY`` per
plan §5.2.1 (Sergey-approved deviation from design §4.0.4 row
5, which defaults claimed-stale to ``MARK_FAILED``):

  1. Mark the stale row ``failed`` (terminal — round-6
     invariant: terminal states are forever; retries are NEW
     rows).
  2. Insert a fresh pending Run row with
     ``parent_run_id=stale.id``,
     ``root_run_id=stale.root_run_id`` (carried),
     ``attempt=stale.attempt + 1``,
     ``fire_reason='retry'``,
     ``due_at=now``.
  3. Append a ``run_failed`` event for the stale row and a
     ``run_retry_scheduled`` event for the new pending row,
     correlated.

Each remediation runs in its own ``transaction(conn)`` block.
A remediation that raises is caught at the boundary: the
helper calls ``logger.error(...)`` AND appends a
``RecoveryError`` to the result list. The scan continues with
the next row — the contract is "never silently drop a stale
row".

The third ``RecoveryPolicy`` value (``CLEAR_CLAIM``) is
reserved for an admin tool that flips a stuck claimed row back
to pending without bumping attempt. Phase 4 does NOT expose
that path.

``run_id_factory`` and ``event_id_factory`` are injected — the
module does NOT call ``uuid.uuid4()`` itself. Production
wiring lands in a later boot-sequence module; tests inject
deterministic counters so the inserted retry rows and paired
events have stable ids assertable from tests.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.1, §4.0.4
- ``docs/PHASE_4_PLAN.md`` §5
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Union

from app.v2.enums import EventKind, FireReason, RecoveryPolicy, RunStatus
from app.v2.runtime.state_machine import assert_legal_transition
from app.v2.storage.connection import assert_connection_ready
from app.v2.storage.serialization import NaiveDatetimeError, encode_json
from app.v2.storage.transactions import transaction


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveredRun:
    """A stale row the scan remediated successfully.

    ``prior_status`` records what the row was (``claimed`` or
    ``running``) before the scan terminated it. ``applied_policy``
    is always ``QUEUE_RETRY`` in phase 4 (the plan §5.2.1
    deviation); the field is kept because admin paths in later
    phases may apply ``MARK_FAILED`` or ``CLEAR_CLAIM``.
    ``new_pending_run_id`` is the id of the freshly inserted
    retry row — None only if a future policy variant skips
    re-queueing (not used in phase 4).
    """

    run_id: str
    schedule_id: str
    prior_status: RunStatus
    applied_policy: RecoveryPolicy
    new_pending_run_id: Optional[str]


@dataclass(frozen=True)
class RecoveryError:
    """A stale row the scan tried to remediate but couldn't.

    Returned alongside ``RecoveredRun`` items so the caller sees
    every failure — the scan never silently drops a remediation.
    The helper also calls ``logger.error(...)`` with the same
    context; the structured result is what the caller iterates
    for follow-up (admin alert, retry, abort boot, etc.).
    """

    run_id: str
    schedule_id: str
    prior_status: RunStatus
    error_message: str


def _to_utc_iso(value: datetime, *, field: str) -> str:
    """UTC-normalise + ISO-8601-serialise a timezone-aware
    datetime. Naive values raise ``NaiveDatetimeError``.

    Same hazard as the storage layer's ``_to_iso_or_none``:
    SQLite compares ISO strings lexically, so mixed offsets
    would break ``ORDER BY claimed_at`` and the cursor predicate
    here. Normalising at the runtime boundary keeps the compare
    sound."""
    if value.tzinfo is None:
        raise NaiveDatetimeError(
            f"naive datetime in {field}: {value!r} — attach "
            "tzinfo (typically datetime.timezone.utc) before "
            "passing."
        )
    return value.astimezone(timezone.utc).isoformat()


def scan_stale_runs(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    claimed_timeout: timedelta,
    running_timeout: timedelta,
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
) -> list[Union[RecoveredRun, RecoveryError]]:
    """Walk the ``runs`` table for stale claimed / running rows
    and remediate each via ``RecoveryPolicy.QUEUE_RETRY``.

    Args:
        conn: caller-owned migrated SQLite connection. NOT
            already inside a transaction (each remediation opens
            its own ``transaction(conn)`` block).
        now: timezone-aware boot timestamp. Used as the cursor
            for both staleness predicates AND the ``due_at`` /
            ``completed_at`` / event ``ts`` of the new rows.
            Naive datetimes raise ``NaiveDatetimeError``.
        claimed_timeout: positive ``timedelta``. A claimed row
            is stale when ``claimed_at < now - claimed_timeout``.
        running_timeout: positive ``timedelta``. A running row
            is stale when
            ``started_at < now - running_timeout`` OR
            ``started_at IS NULL``.
        run_id_factory: callable returning a fresh id for each
            new pending Run row inserted by the scan. Injected
            so tests can pin deterministic ids; production
            wiring uses ``uuid.uuid4().hex``.
        event_id_factory: callable returning a fresh id for
            each ``run_failed`` / ``run_retry_scheduled`` event
            row inserted by the scan. Same rationale as
            ``run_id_factory``.

    Returns:
        Ordered list of ``RecoveredRun`` (success) and
        ``RecoveryError`` (per-row failure) items. Order is
        deterministic: claimed-stale rows first (sorted by
        ``claimed_at, id``), then running-stale rows (sorted
        by ``started_at, id``; NULL ``started_at`` rows sort
        first because SQLite ASC places NULL ahead of any
        value).

    Raises:
        NaiveDatetimeError: ``now`` was naive (raised before any
            SQL runs).
        ConnectionNotReady: bad connection state.

    Does NOT raise on a per-row remediation failure — those
    surface as ``RecoveryError`` items + a ``logger.error`` line.
    """
    assert_connection_ready(conn)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo is not None else None
    if now_utc is None:
        # _to_utc_iso would raise from inside, but we'd rather
        # raise BEFORE computing the cutoffs so the error path
        # is identical to claim_run's.
        raise NaiveDatetimeError(
            f"naive datetime in now: {now!r} — attach tzinfo "
            "(typically datetime.timezone.utc) before passing."
        )
    now_iso = now_utc.isoformat()
    claimed_cutoff = (now_utc - claimed_timeout).isoformat()
    running_cutoff = (now_utc - running_timeout).isoformat()

    # SQLite ASC sorts NULL FIRST — that's exactly what the
    # running-stale invariant-defensive case wants: rows with
    # NULL started_at sort ahead of dated ones.
    claimed_rows = conn.execute(
        "SELECT id, schedule_id, root_run_id, attempt "
        "FROM runs "
        "WHERE status = ? AND claimed_at < ? "
        "ORDER BY claimed_at ASC, id ASC",
        (RunStatus.CLAIMED.value, claimed_cutoff),
    ).fetchall()

    running_rows = conn.execute(
        "SELECT id, schedule_id, root_run_id, attempt "
        "FROM runs "
        "WHERE status = ? "
        "AND (started_at < ? OR started_at IS NULL) "
        "ORDER BY started_at ASC, id ASC",
        (RunStatus.RUNNING.value, running_cutoff),
    ).fetchall()

    results: list[Union[RecoveredRun, RecoveryError]] = []
    for row in claimed_rows:
        results.append(
            _remediate(
                conn,
                row=row,
                prior_status=RunStatus.CLAIMED,
                now_iso=now_iso,
                run_id_factory=run_id_factory,
                event_id_factory=event_id_factory,
            )
        )
    for row in running_rows:
        results.append(
            _remediate(
                conn,
                row=row,
                prior_status=RunStatus.RUNNING,
                now_iso=now_iso,
                run_id_factory=run_id_factory,
                event_id_factory=event_id_factory,
            )
        )
    return results


def _remediate(
    conn: sqlite3.Connection,
    *,
    row: tuple,
    prior_status: RunStatus,
    now_iso: str,
    run_id_factory: Callable[[], str],
    event_id_factory: Callable[[], str],
) -> Union[RecoveredRun, RecoveryError]:
    """Two-row remediation inside one transaction.

    Returns a ``RecoveredRun`` on success or a ``RecoveryError``
    when any step raises. The transaction context manager rolls
    back automatically on exception; the caught error surfaces
    as the result-list item.
    """
    stale_run_id, schedule_id, root_run_id, attempt = row
    try:
        # Pin the state-machine policy: claimed→failed and
        # running→failed are both legal phase-4 transitions
        # (verified by tests/v2/test_runtime_state_machine.py).
        # Calling here means a future narrowing of
        # LEGAL_TRANSITIONS surfaces at recovery time instead of
        # silently writing an illegal transition.
        assert_legal_transition(prior_status, RunStatus.FAILED)

        new_pending_id = run_id_factory()
        failed_event_id = event_id_factory()
        retry_event_id = event_id_factory()

        with transaction(conn):
            # 1. Mark the stale row terminal. Status guard in
            #    WHERE pins that the row really is in the prior
            #    status — defends against a concurrent scan
            #    interleaving (not expected at boot, but the
            #    guard is cheap and loud if the assumption ever
            #    breaks).
            cursor = conn.execute(
                "UPDATE runs SET status = ?, completed_at = ?, "
                "error = ? WHERE id = ? AND status = ?",
                (
                    RunStatus.FAILED.value,
                    now_iso,
                    f"boot recovery: stale {prior_status.value}",
                    stale_run_id,
                    prior_status.value,
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError(
                    f"stale row {stale_run_id!r} status changed "
                    f"between SELECT and UPDATE (expected "
                    f"{prior_status.value!r}); refusing to "
                    "remediate."
                )

            # 2. Append run_failed event for the stale row.
            conn.execute(
                "INSERT INTO events "
                "(id, run_id, schedule_id, ts, kind, "
                " payload_json, correlates) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    failed_event_id,
                    stale_run_id,
                    schedule_id,
                    now_iso,
                    EventKind.RUN_FAILED.value,
                    encode_json(
                        {
                            "reason": "boot_recovery_stale",
                            "prior_status": prior_status.value,
                        }
                    ),
                    None,
                ),
            )

            # 3. Insert a fresh pending Run row carrying the
            #    retry-chain links. The Pydantic Run model's
            #    self-reference / parent_run_id invariants are
            #    satisfied by construction: attempt+1 >= 2,
            #    parent_run_id is set, root_run_id is the
            #    original chain root (not the new id).
            conn.execute(
                "INSERT INTO runs "
                "(id, schedule_id, fire_reason, due_at, status, "
                " attempt, root_run_id, parent_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_pending_id,
                    schedule_id,
                    FireReason.RETRY.value,
                    now_iso,
                    RunStatus.PENDING.value,
                    attempt + 1,
                    root_run_id,
                    stale_run_id,
                ),
            )

            # 4. Append run_retry_scheduled event for the new
            #    pending row, correlated to the failure event.
            conn.execute(
                "INSERT INTO events "
                "(id, run_id, schedule_id, ts, kind, "
                " payload_json, correlates) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    retry_event_id,
                    new_pending_id,
                    schedule_id,
                    now_iso,
                    EventKind.RUN_RETRY_SCHEDULED.value,
                    encode_json(
                        {
                            "retry_of": stale_run_id,
                            "policy": RecoveryPolicy.QUEUE_RETRY.value,
                            "attempt": attempt + 1,
                        }
                    ),
                    failed_event_id,
                ),
            )
        return RecoveredRun(
            run_id=stale_run_id,
            schedule_id=schedule_id,
            prior_status=prior_status,
            applied_policy=RecoveryPolicy.QUEUE_RETRY,
            new_pending_run_id=new_pending_id,
        )
    except Exception as exc:
        _logger.error(
            "recovery remediation failed for run_id=%s "
            "schedule_id=%s prior_status=%s: %s",
            stale_run_id,
            schedule_id,
            prior_status.value,
            exc,
        )
        return RecoveryError(
            run_id=stale_run_id,
            schedule_id=schedule_id,
            prior_status=prior_status,
            error_message=str(exc),
        )


__all__ = [
    "RecoveredRun",
    "RecoveryError",
    "scan_stale_runs",
]
