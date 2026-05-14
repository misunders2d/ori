"""SQL DDL files for the v2 scheduler.

Each `v<NNN>_<label>.sql` file is the canonical schema for one
forward-only migration. Files are immutable once shipped — a new
schema change is a new migration file (see
``docs/PHASE_1_PLAN.md`` §3.4).

The matching ``app/v2/migrations/v<NNN>_<label>.py`` is the
Python loader that reads the .sql file and executes its
statements against a SQLite connection.

What lives here:
- v2 ``CREATE TABLE`` and ``CREATE INDEX`` statements only.

What does NOT live here (owned by the runner):
- ``applied_migrations`` bookkeeping table — created by
  ``app.v2.migrations.runner`` outside the per-migration TX.
- ``PRAGMA journal_mode=WAL`` — SQLite forbids journal-mode
  changes inside a transaction, so the runner sets it before
  opening any migration TX.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2
- ``docs/PHASE_1_PLAN.md`` §3.2, §3.5
"""
