"""Pytest fixtures shared across ``tests/v2/``.

Phase 1 fixtures are minimal — most tests instantiate Pydantic
models directly with literal values. The fixture set grows as
later phase-1 commits add the storage layer, the ScheduleSpec
builder helpers, and the SQLite DB target.

Future fixtures (phase-1 commits 3+):
- ``tmp_db`` — per-test SQLite DB at ``tmp_path``, pre-migrated.
- ``sample_schedule_spec`` — builder for a valid ScheduleSpec.
- ``sample_execution_plan`` — builder for a minimal ExecutionPlan.
- ``sample_run`` — Run at any status.
- ``sample_event`` — Event of any kind.

Kept intentionally tiny in this commit so nothing here depends
on models that don't yet exist.
"""
