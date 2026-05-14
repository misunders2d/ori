"""Tests for ``app.v2.idempotency.compute_idempotency_key``.

The whole point of the v2 idempotency design is that retries of
the same logical fire produce the SAME key so a prior
``emit_succeeded`` ledger row short-circuits the second post.
The round-6 review specifically called out a regression in an
earlier draft that included ``attempt`` in the key — every
retry then had a different key and dedup did nothing.

Properties pinned:

1. Same logical fire (same schedule_id + root_run_id + emit_id)
   → same key, regardless of ``attempt``, ``run_id``, ``due_at``,
   ``status`` history.
2. Different emit_id → different key (per-emit dedup).
3. Different root_run_id → different key (per-logical-fire
   namespace).
4. Different schedule_id → different key (per-schedule
   namespace).
5. The function rejects empty values and values containing the
   delimiter ``:``.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §6.4
- ``docs/PHASE_1_PLAN.md`` §5.2
"""

from __future__ import annotations

import pytest

from app.v2.idempotency import compute_idempotency_key


# ---------------------------------------------------------------------------
# Format + determinism
# ---------------------------------------------------------------------------


def test_format_is_three_colon_joined_parts():
    key = compute_idempotency_key(
        schedule_id="daily_audit",
        root_run_id="root-abc",
        emit_id="post_slack",
    )
    assert key == "daily_audit:root-abc:post_slack"


def test_key_is_deterministic():
    k1 = compute_idempotency_key(
        schedule_id="x", root_run_id="r", emit_id="e"
    )
    k2 = compute_idempotency_key(
        schedule_id="x", root_run_id="r", emit_id="e"
    )
    assert k1 == k2


# ---------------------------------------------------------------------------
# Stability across retries (round-6 regression)
# ---------------------------------------------------------------------------


def test_key_stable_across_retries_with_same_root_run_id():
    """The defining property: a retry produces the same key.
    A retry's Run.id differs from the original's; root_run_id
    is the stable anchor. The function takes root_run_id, NOT
    run_id, so the same logical fire's emits always dedup."""
    original_run_id = "r-original"
    retry_run_id = "r-retry-2"
    key_original = compute_idempotency_key(
        schedule_id="s",
        root_run_id=original_run_id,
        emit_id="post_slack",
    )
    key_retry = compute_idempotency_key(
        schedule_id="s",
        root_run_id=original_run_id,
        emit_id="post_slack",
    )
    assert key_original == key_retry

    # And distinct from the run_id (just to be loud about it):
    accidental_key_with_run_id = compute_idempotency_key(
        schedule_id="s",
        root_run_id=retry_run_id,
        emit_id="post_slack",
    )
    assert accidental_key_with_run_id != key_original


# ---------------------------------------------------------------------------
# Per-emit dedup
# ---------------------------------------------------------------------------


def test_different_emit_ids_produce_different_keys():
    """A schedule with two emits ('post_slack' and 'log_sheet')
    must not have one emit's success short-circuit the other.
    Per-emit_id namespacing keeps them independent."""
    k_slack = compute_idempotency_key(
        schedule_id="s", root_run_id="r", emit_id="post_slack"
    )
    k_sheet = compute_idempotency_key(
        schedule_id="s", root_run_id="r", emit_id="log_sheet"
    )
    assert k_slack != k_sheet


# ---------------------------------------------------------------------------
# Per-fire / per-schedule namespacing
# ---------------------------------------------------------------------------


def test_different_root_run_ids_produce_different_keys():
    """Tomorrow's fire of the same schedule must produce a
    different key (different root_run_id) — otherwise tomorrow's
    post would dedup against yesterday's."""
    k_today = compute_idempotency_key(
        schedule_id="s", root_run_id="fire_today", emit_id="post"
    )
    k_tomorrow = compute_idempotency_key(
        schedule_id="s", root_run_id="fire_tomorrow", emit_id="post"
    )
    assert k_today != k_tomorrow


def test_different_schedule_ids_produce_different_keys():
    """Two schedules both with an emit called 'post_slack' must
    not collide. Schedule-id is the outer namespace."""
    k_a = compute_idempotency_key(
        schedule_id="sched_a", root_run_id="r", emit_id="post_slack"
    )
    k_b = compute_idempotency_key(
        schedule_id="sched_b", root_run_id="r", emit_id="post_slack"
    )
    assert k_a != k_b


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["schedule_id", "root_run_id", "emit_id"])
def test_empty_components_rejected(field):
    """Empty strings would produce keys like 'a::b' which
    silently collide. Reject loud."""
    kwargs = {
        "schedule_id": "a",
        "root_run_id": "b",
        "emit_id": "c",
    }
    kwargs[field] = ""
    with pytest.raises(ValueError, match=field):
        compute_idempotency_key(**kwargs)


@pytest.mark.parametrize("field", ["schedule_id", "root_run_id", "emit_id"])
def test_colon_in_components_rejected(field):
    """Colons in any component would shift the delimiters and
    let unrelated values collide. Sanity-check rejection."""
    kwargs = {
        "schedule_id": "a",
        "root_run_id": "b",
        "emit_id": "c",
    }
    kwargs[field] = "value:with:colons"
    with pytest.raises(ValueError, match=field):
        compute_idempotency_key(**kwargs)
