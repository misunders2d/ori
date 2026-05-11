"""Tests for the disk-backed sandbox cycle marker introduced in Phase 2.

The cycle marker is what lets an evolution survive a `/reset session` or
a process restart mid-cycle. Previously the state lived in
`tool_context.state["evolution_cycle_active"]`, which vanished with the
session and tripped a spurious sandbox wipe on the next stage attempt.
"""

import os
import time

import pytest


@pytest.fixture
def fresh_sandbox(tmp_path):
    """Return a tmp path acting as the sandbox dir; clean up after."""
    sandbox = tmp_path / "sandbox"
    yield str(sandbox)


def test_cycle_marker_absent_when_dir_missing(fresh_sandbox):
    """No directory → not fresh."""
    from app.tools.evolution import _sandbox_cycle_is_fresh

    assert _sandbox_cycle_is_fresh(fresh_sandbox) is False


def test_cycle_begin_creates_marker(fresh_sandbox):
    """begin() should leave a `.cycle_active` file behind."""
    from app.tools.evolution import _sandbox_cycle_begin, _sandbox_cycle_marker_path

    _sandbox_cycle_begin(fresh_sandbox)

    assert os.path.isdir(fresh_sandbox)
    assert os.path.isfile(_sandbox_cycle_marker_path(fresh_sandbox))


def test_cycle_fresh_after_begin(fresh_sandbox):
    """A just-started cycle is fresh."""
    from app.tools.evolution import _sandbox_cycle_begin, _sandbox_cycle_is_fresh

    _sandbox_cycle_begin(fresh_sandbox)

    assert _sandbox_cycle_is_fresh(fresh_sandbox) is True


def test_cycle_end_wipes_marker_and_dir(fresh_sandbox):
    """end() should wipe both the dir and the marker (no leftovers)."""
    from app.tools.evolution import _sandbox_cycle_begin, _sandbox_cycle_end, _sandbox_cycle_is_fresh

    _sandbox_cycle_begin(fresh_sandbox)
    _sandbox_cycle_end(fresh_sandbox)

    assert not os.path.exists(fresh_sandbox)
    assert _sandbox_cycle_is_fresh(fresh_sandbox) is False


def test_cycle_stale_when_marker_expired(fresh_sandbox, monkeypatch):
    """Marker older than TTL is treated as abandoned (stale)."""
    from app.tools import evolution as ev

    ev._sandbox_cycle_begin(fresh_sandbox)
    marker = ev._sandbox_cycle_marker_path(fresh_sandbox)

    # Backdate the marker beyond TTL.
    stale_time = time.time() - (ev._SANDBOX_CYCLE_TTL_SECONDS + 60)
    os.utime(marker, (stale_time, stale_time))

    assert ev._sandbox_cycle_is_fresh(fresh_sandbox) is False


def test_cycle_begin_wipes_pre_existing_content(fresh_sandbox):
    """begin() always starts clean — no leftover files from a prior cycle."""
    from app.tools.evolution import _sandbox_cycle_begin

    os.makedirs(fresh_sandbox)
    with open(os.path.join(fresh_sandbox, "stale.py"), "w") as f:
        f.write("# leftover from prior aborted cycle")

    _sandbox_cycle_begin(fresh_sandbox)

    assert not os.path.exists(os.path.join(fresh_sandbox, "stale.py"))


def test_marker_survives_simulated_restart(fresh_sandbox):
    """Re-evaluating freshness after a 'restart' (just re-call the helper)
    still sees the cycle as fresh — no in-memory state involved."""
    from app.tools.evolution import _sandbox_cycle_begin, _sandbox_cycle_is_fresh

    _sandbox_cycle_begin(fresh_sandbox)
    assert _sandbox_cycle_is_fresh(fresh_sandbox) is True

    # Imagine the process exited here and a new one is asking again.
    # Nothing in memory, only the on-disk marker.
    assert _sandbox_cycle_is_fresh(fresh_sandbox) is True
