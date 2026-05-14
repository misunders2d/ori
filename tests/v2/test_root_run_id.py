"""Tests for ``root_run_id`` behaviour across a multi-attempt
retry chain.

These tests are layered on top of ``test_models_run.py`` — that
file pins the single-Run validators. This file pins the
INTER-Run invariants that hold across a sequence of attempts:

- First attempt: ``id == root_run_id``, ``parent_run_id is None``.
- Second attempt: distinct ``id``, same ``root_run_id`` as
  attempt 1, ``parent_run_id`` = attempt 1's id.
- Third attempt: distinct ``id``, same ``root_run_id`` as the
  whole chain, ``parent_run_id`` = attempt 2's id.
- Walking ``parent_run_id`` back from any retry returns the full
  chain in reverse.
- All attempts in a chain share the same ``schedule_id`` and
  the same ``root_run_id``.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 (retry chain invariant)
- ``docs/PHASE_1_PLAN.md`` §5.2 (root_run_id self-ref + chain
  propagation)
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.v2.enums import FireReason, RunStatus
from app.v2.models.run import Run


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


def _build_chain(
    num_attempts: int,
    schedule_id: str = "daily_audit",
    id_prefix: str = "run",
) -> list[Run]:
    """Build a synthetic retry chain of length ``num_attempts``.

    ``id_prefix`` lets the caller distinguish independent fires
    of the same schedule (in production these are uuid4 strings;
    here we use prefixed positional ids so two test chains don't
    collide on the position-based ids).

    Returns the list in attempt order. Each Run is fully valid
    per the single-Run validators in run.py — first-attempt
    self-reference + retry parent/root invariants.
    """
    runs: list[Run] = []
    for i in range(1, num_attempts + 1):
        run_id = f"{id_prefix}-{i:02d}"
        if i == 1:
            root = run_id
            parent = None
        else:
            root = runs[0].id
            parent = runs[i - 2].id
        runs.append(
            Run(
                id=run_id,
                schedule_id=schedule_id,
                execution_plan_hash="a" * 64,
                fire_reason=(
                    FireReason.SCHEDULED if i == 1 else FireReason.RETRY
                ),
                due_at=_NOW,
                status=RunStatus.PENDING,
                attempt=i,
                root_run_id=root,
                parent_run_id=parent,
            )
        )
    return runs


# ---------------------------------------------------------------------------
# First-attempt self-reference
# ---------------------------------------------------------------------------


def test_first_attempt_id_equals_root_run_id():
    chain = _build_chain(1)
    first = chain[0]
    assert first.id == first.root_run_id
    assert first.parent_run_id is None
    assert first.attempt == 1


def test_first_attempt_uses_scheduled_fire_reason():
    """Sanity: a fresh first attempt is ``scheduled`` (or
    ``manual`` / ``backfill`` / ``replay``), never ``retry``.
    Retry is what subsequent attempts use."""
    chain = _build_chain(1)
    assert chain[0].fire_reason == FireReason.SCHEDULED


# ---------------------------------------------------------------------------
# Second + third attempts propagate root + chain via parent
# ---------------------------------------------------------------------------


def test_second_attempt_shares_root_with_first():
    chain = _build_chain(2)
    first, retry = chain
    assert retry.root_run_id == first.id
    assert retry.id != first.id
    assert retry.parent_run_id == first.id
    assert retry.attempt == 2
    assert retry.fire_reason == FireReason.RETRY


def test_third_attempt_shares_root_with_first_chains_via_parent():
    chain = _build_chain(3)
    first, second, third = chain
    # root is the FIRST attempt's id throughout the chain
    assert second.root_run_id == first.id
    assert third.root_run_id == first.id
    # parent chains one step at a time
    assert second.parent_run_id == first.id
    assert third.parent_run_id == second.id
    # ids are all distinct
    assert len({first.id, second.id, third.id}) == 3


# ---------------------------------------------------------------------------
# Retry-chain reverse walk via parent_run_id
# ---------------------------------------------------------------------------


def test_walk_parent_chain_returns_full_chain_in_reverse():
    """Given the leaf retry, walking parent_run_id back reaches
    the first attempt. This is the query the observability tools
    will run for ``contract_history(schedule_id)``."""
    chain = _build_chain(4)
    by_id = {r.id: r for r in chain}
    leaf = chain[-1]

    walked: list[str] = [leaf.id]
    current = leaf
    while current.parent_run_id is not None:
        current = by_id[current.parent_run_id]
        walked.append(current.id)

    expected = [r.id for r in reversed(chain)]
    assert walked == expected


def test_chain_all_share_schedule_id():
    chain = _build_chain(5, schedule_id="fba_audit")
    for r in chain:
        assert r.schedule_id == "fba_audit"


def test_chain_all_share_root_run_id():
    chain = _build_chain(5)
    root = chain[0].id
    for r in chain:
        assert r.root_run_id == root


# ---------------------------------------------------------------------------
# A two-fires-of-the-same-schedule scenario keeps the chains
# DISTINCT — retries from yesterday's fire don't mingle with
# retries from today's fire.
# ---------------------------------------------------------------------------


def test_two_separate_fires_have_distinct_chains():
    """Two days of the same daily schedule produce two separate
    retry chains. The roots differ; idempotency keys for emits
    in each chain therefore differ (per
    ``test_idempotency_key.py``). ``id_prefix`` makes the
    synthetic test reflect production uuid4 distinctness."""
    yesterday = _build_chain(
        2, schedule_id="daily_audit", id_prefix="yesterday"
    )
    today = _build_chain(2, schedule_id="daily_audit", id_prefix="today")

    y_first, y_retry = yesterday
    t_first, t_retry = today

    # Per-chain invariants hold inside each fire.
    assert y_retry.root_run_id == y_first.id
    assert t_retry.root_run_id == t_first.id

    # The two fires don't share ids — yesterday's retry doesn't
    # claim today's first attempt as its parent.
    assert y_retry.parent_run_id != t_first.id
    assert y_retry.root_run_id != t_first.id
    assert y_first.id != t_first.id
