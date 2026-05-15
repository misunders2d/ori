"""Pytest fixtures + marker registrations for ``tests/v2/``.

Phase 1 fixtures are minimal — most tests instantiate Pydantic
models directly with literal values. The fixture set grows as
later phase-1 commits add the storage layer, the ScheduleSpec
builder helpers, and the SQLite DB target.

Phase 5 slice 7b registers the ``slow`` marker locally so
``--strict-markers`` (set in ``pyproject.toml``) accepts
``@pytest.mark.slow`` on ``test_runtime_e2e_slow.py`` without
touching ``pyproject.toml`` (out of ``PHASE_ALLOWLIST[5]``).
Slow-marked tests are excluded from the required fast CI lane
via ``pytest -m "not slow"`` and exercised in nightly /
pre-release smoke.

Future fixtures (phase-1 commits 3+):
- ``tmp_db`` — per-test SQLite DB at ``tmp_path``, pre-migrated.
- ``sample_schedule_spec`` — builder for a valid ScheduleSpec.
- ``sample_execution_plan`` — builder for a minimal ExecutionPlan.
- ``sample_run`` — Run at any status.
- ``sample_event`` — Event of any kind.

Kept intentionally tiny in this commit so nothing here depends
on models that don't yet exist.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: wall-clock-dependent integration test; excluded "
        "from required fast CI via ``pytest -m 'not slow'``. "
        "Runs in nightly / pre-release smoke.",
    )
