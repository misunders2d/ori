"""Tests for ``app.v2.runtime.state_machine``.

Pins per ``docs/PHASE_4_PLAN.md`` §3 + §3.1 + §3.1.1:

- Every entry in ``LEGAL_TRANSITIONS`` returns True from
  ``is_legal_transition`` and does NOT raise from
  ``assert_legal_transition``.
- Remaining future transitions §3.1.1 (claimed→pending,
  running→pending) are NOT in ``LEGAL_TRANSITIONS`` and
  explicitly raise from ``assert_legal_transition``.
  Phase-7 slice 5a admitted ``(PENDING, CANCELLED)`` to
  the legal set for the archive helper.
- Any other illegal pair (terminal → anything, self-loops,
  pending → succeeded etc.) raises.
- ``IllegalTransitionError`` is a ``ValueError`` subclass.
- ``LEGAL_TRANSITIONS`` is the exact 6-element set.
"""

from __future__ import annotations

import pytest

from app.v2.enums import RunStatus
from app.v2.runtime.state_machine import (
    IllegalTransitionError,
    LEGAL_TRANSITIONS,
    assert_legal_transition,
    is_legal_transition,
)


# ---------------------------------------------------------------------------
# Canonical sets
# ---------------------------------------------------------------------------


_EXPECTED_LEGAL = frozenset(
    {
        (RunStatus.PENDING, RunStatus.CLAIMED),
        (RunStatus.PENDING, RunStatus.CANCELLED),  # phase-7 slice 5a
        (RunStatus.CLAIMED, RunStatus.RUNNING),
        (RunStatus.CLAIMED, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.SUCCEEDED),
        (RunStatus.RUNNING, RunStatus.FAILED),
    }
)

# Future transitions per plan §3.1.1 — these MUST NOT appear in
# LEGAL_TRANSITIONS, otherwise the worker / recovery scan
# could perform them silently. Phase-7 slice 5a removed
# ``(PENDING, CANCELLED)`` from this list because the archive
# helper now owns that transition through the chokepoint.
_FUTURE_FORBIDDEN_PAIRS = [
    (RunStatus.CLAIMED, RunStatus.PENDING),    # CLEAR_CLAIM admin tool
    (RunStatus.RUNNING, RunStatus.PENDING),    # retry chain re-status
]


# ---------------------------------------------------------------------------
# LEGAL_TRANSITIONS shape pin
# ---------------------------------------------------------------------------


def test_legal_transitions_is_exact_six_entry_set():
    """Reviewer round-3 approved a 5-entry table for phase 4.
    Phase-7 slice 5a added the ``(PENDING, CANCELLED)``
    transition for the archive helper, bringing the set to
    six entries. Any further drift must be reviewer-approved
    + documented in the relevant plan."""
    assert LEGAL_TRANSITIONS == _EXPECTED_LEGAL


def test_legal_transitions_is_frozen():
    """The set is shared module state; tests rely on it being
    immutable so a misbehaving call site can't mutate it."""
    assert isinstance(LEGAL_TRANSITIONS, frozenset)


def test_legal_transitions_size_is_six():
    """Explicit size pin — independent of the contents check —
    so a future regression that swaps an entry rather than
    adds/removes one still shows up here."""
    assert len(LEGAL_TRANSITIONS) == 6


# ---------------------------------------------------------------------------
# Legal transitions accept paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src, dst",
    sorted(_EXPECTED_LEGAL, key=lambda p: (p[0].value, p[1].value)),
)
def test_is_legal_transition_true_for_each_legal_pair(src, dst):
    assert is_legal_transition(src, dst) is True


@pytest.mark.parametrize(
    "src, dst",
    sorted(_EXPECTED_LEGAL, key=lambda p: (p[0].value, p[1].value)),
)
def test_assert_legal_transition_does_not_raise_for_legal_pair(src, dst):
    # Should return None without raising.
    assert assert_legal_transition(src, dst) is None


# ---------------------------------------------------------------------------
# Future transitions (§3.1.1) must reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("src, dst", _FUTURE_FORBIDDEN_PAIRS)
def test_future_transitions_not_legal(src, dst):
    """§3.1.1: pending→cancelled, claimed→pending,
    running→pending are documented as future paths and have
    no phase-4 owner. Phase 4 LEGAL_TRANSITIONS must NOT
    contain them."""
    assert (src, dst) not in LEGAL_TRANSITIONS
    assert is_legal_transition(src, dst) is False


@pytest.mark.parametrize("src, dst", _FUTURE_FORBIDDEN_PAIRS)
def test_future_transitions_raise(src, dst):
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition(src, dst)


# ---------------------------------------------------------------------------
# Other illegal pairs (terminal → anything, self-loops,
# pending → succeeded, etc.)
# ---------------------------------------------------------------------------


_TERMINAL = (
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
)


@pytest.mark.parametrize("terminal", _TERMINAL)
@pytest.mark.parametrize("dst", list(RunStatus))
def test_terminal_states_cannot_transition_anywhere(terminal, dst):
    """Terminal states are forever (round-6 invariant). The
    state machine refuses every outgoing transition from
    succeeded / failed / cancelled, including back to itself."""
    assert is_legal_transition(terminal, dst) is False
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition(terminal, dst)


@pytest.mark.parametrize("status", list(RunStatus))
def test_self_loops_rejected(status):
    """No status → same status. The data plane would accept
    the unpredicated UPDATE; the state machine refuses."""
    assert is_legal_transition(status, status) is False
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition(status, status)


@pytest.mark.parametrize(
    "src, dst",
    [
        # "skip" transitions that bypass the claim step.
        (RunStatus.PENDING, RunStatus.RUNNING),
        (RunStatus.PENDING, RunStatus.SUCCEEDED),
        (RunStatus.PENDING, RunStatus.FAILED),
        # Claimed cannot succeed without going through running.
        (RunStatus.CLAIMED, RunStatus.SUCCEEDED),
        (RunStatus.CLAIMED, RunStatus.CANCELLED),
    ],
)
def test_other_illegal_pairs_rejected(src, dst):
    assert is_legal_transition(src, dst) is False
    with pytest.raises(IllegalTransitionError):
        assert_legal_transition(src, dst)


# ---------------------------------------------------------------------------
# Exhaustive coverage: every (src, dst) is either in the
# expected legal set OR rejected. No silently-ignored pair.
# ---------------------------------------------------------------------------


def test_every_status_pair_is_classified():
    """For every (src, dst) in the cross-product of RunStatus,
    the pair is either explicitly legal (in
    LEGAL_TRANSITIONS) or explicitly illegal. Nothing is
    silently neutral — the predicate's answer matches the
    assertion helper's behavior."""
    for src in RunStatus:
        for dst in RunStatus:
            legal = is_legal_transition(src, dst)
            if legal:
                # Should not raise.
                assert_legal_transition(src, dst)
            else:
                with pytest.raises(IllegalTransitionError):
                    assert_legal_transition(src, dst)


# ---------------------------------------------------------------------------
# Error hierarchy + message
# ---------------------------------------------------------------------------


def test_illegal_transition_error_is_value_error():
    assert issubclass(IllegalTransitionError, ValueError)


def test_illegal_transition_error_message_contains_both_sides():
    """The error message should pinpoint both endpoints so an
    operator reading a log knows which transition was
    attempted."""
    with pytest.raises(IllegalTransitionError) as exc_info:
        assert_legal_transition(RunStatus.SUCCEEDED, RunStatus.PENDING)
    msg = str(exc_info.value)
    assert "succeeded" in msg
    assert "pending" in msg
