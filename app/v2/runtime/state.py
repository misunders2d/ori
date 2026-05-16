"""§6.5 cross-fire state RUNTIME primitives (phase-13 slice-1).

Implements ``docs/CONTRACTS_V2_DESIGN.md`` §6.5 + the
claude-reviewer phase-13 plan §9 (Q1=(a) / Q2–Q6) dispositions.

A THIN, typed, DI-connection layer over the SHIPPED phase-3
storage compare-and-set primitives
(:func:`app.v2.storage.schedule_state.get_state` /
:func:`~app.v2.storage.schedule_state.set_state_cas`). It
re-implements **no SQL**, touches **no DDL**, and leaves
``app/v2/storage/schedule_state.py`` byte-untouched — it only
maps the §6.5 runtime contract onto the storage primitive and
returns a typed outcome.

- ``state_read(conn, schedule_id, key)`` →
  :class:`StateView` ``{value, version, written_at,
  written_by_run}`` or ``None`` (thin map over ``get_state`` /
  ``ScheduleState``).
- ``state_write(conn, schedule_id, key, value, written_by_run,
  now, expected_version=None)`` → :class:`StateWriteOutcome`.

  ``expected_version`` mapping (§6.5: *"if expected_version
  provided, refuse unless current version matches (CAS)"*):
  - ``None`` (or ``0``) — first-write / seed: delegates to
    ``set_state_cas(expected_version=0)`` (``INSERT OR
    IGNORE``). A row already present ⇒ ``stale_version`` (NOT
    an unconditional overwrite — there is no read-then-write
    here, so no TOCTOU; to replace an existing row the caller
    reads the version and passes it as a CAS).
  - ``>= 1`` — CAS update: delegates to
    ``set_state_cas(expected_version=…)``.

  **NO internal retry / NO spin** — a stale version returns a
  typed ``stale_version`` outcome IMMEDIATELY; the caller owns
  the retry policy (the shipped phase-3 contract). On
  ``stale_version`` ``version`` is ``None`` (no hidden extra
  read — the caller re-reads via ``state_read`` and decides).
  ``StateRunMismatchError`` (lineage audit-truth),
  ``NaiveDatetimeError`` (naive ``now``), ``ValueError``
  (``expected_version < 0``), ``sqlite3.IntegrityError``
  (ghost ``written_by_run``) and ``ConnectionNotReady`` all
  PROPAGATE — never swallowed.

The LLM never touches state (§6.5) — these are called by
deterministic loader logic. No ``datetime.now`` / ``uuid4`` /
vendor SDK at module load (``now`` is DI-passed). ZERO
sources / resolver / cache / emit / worker touch — the
build-the-layer worker seam is slice 2.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §6.5, §12 step 13, D5
- ``docs/PHASE_13_PLAN.md`` §1, §3, §9 (Q1–Q6)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Optional

from app.v2.storage.schedule_state import get_state, set_state_cas

if TYPE_CHECKING:  # pragma: no cover - typing only
    import sqlite3
    from datetime import datetime


@dataclass(frozen=True)
class StateView:
    """The §6.5 ``state_read`` shape: the decoded value + the
    CAS token + lineage. ``schedule_id`` / ``key`` are the
    lookup arguments, deliberately not echoed here."""

    value: Any
    version: int
    written_at: "datetime"
    written_by_run: Optional[str]


@dataclass(frozen=True)
class StateWriteOutcome:
    """Typed result of :func:`state_write`.

    - ``status == "written"`` — the value was persisted;
      ``version`` is the NEW on-disk version (``1`` for a
      first write, ``expected_version + 1`` for a CAS update).
    - ``status == "stale_version"`` — the write was REFUSED
      (the row already exists for a first-write, or the CAS
      version did not match). ``version`` is ``None`` — the
      caller re-reads via :func:`state_read` and decides
      (NO internal retry; the phase-3 caller-owns-retry
      contract).
    """

    status: Literal["written", "stale_version"]
    version: Optional[int] = None


def state_read(
    conn: "sqlite3.Connection",
    *,
    schedule_id: str,
    key: str,
) -> Optional[StateView]:
    """Read ``(schedule_id, key)`` cross-fire state.

    Thin map over ``get_state`` — returns ``None`` when absent,
    else the §6.5 view. Read-only; never mutates."""
    state = get_state(conn, schedule_id=schedule_id, key=key)
    if state is None:
        return None
    return StateView(
        value=state.value,
        version=state.version,
        written_at=state.written_at,
        written_by_run=state.written_by_run,
    )


def state_write(
    conn: "sqlite3.Connection",
    *,
    schedule_id: str,
    key: str,
    value: Any,
    written_by_run: Optional[str],
    now: "datetime",
    expected_version: Optional[int] = None,
) -> StateWriteOutcome:
    """Write ``(schedule_id, key)`` cross-fire state (§6.5).

    ``expected_version=None`` (or ``0``) → first-write / seed;
    ``>= 1`` → CAS update. Delegates to ``set_state_cas`` (no
    SQL here). A refused write (stale version / first-write
    collision) returns ``stale_version`` IMMEDIATELY — no
    internal retry/spin. All storage-layer exceptions
    propagate unchanged.
    """
    # ``None`` ⇒ the storage primitive's first-write sentinel
    # (``expected_version == 0`` → INSERT OR IGNORE). A
    # negative value is left for ``set_state_cas`` to reject
    # (ValueError) — not silently coerced.
    storage_expected = 0 if expected_version is None else expected_version

    ok = set_state_cas(
        conn,
        schedule_id=schedule_id,
        key=key,
        new_value=value,
        expected_version=storage_expected,
        written_by_run=written_by_run,
        now=now,
    )
    if not ok:
        # Stale version OR first-write collision. NO spin —
        # caller re-reads + decides (phase-3 contract).
        return StateWriteOutcome(status="stale_version")
    new_version = (
        1 if storage_expected == 0 else storage_expected + 1
    )
    return StateWriteOutcome(status="written", version=new_version)


__all__ = [
    "StateView",
    "StateWriteOutcome",
    "state_read",
    "state_write",
]
