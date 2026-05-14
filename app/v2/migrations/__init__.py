"""Forward-only SQLite migrations for the v2 scheduler.

A migration is a Python module exposing one ``Migration`` whose
``apply(conn)`` executes the corresponding ``.sql`` file from
``app/v2/ddl/`` against the given SQLite connection.

The runner (``app.v2.migrations.runner``) is the only caller of
``apply``. It owns:

- ``PRAGMA journal_mode=WAL`` (executed OUTSIDE any transaction,
  because SQLite forbids journal-mode changes inside a TX).
- ``PRAGMA foreign_keys=ON`` (per-connection).
- The ``applied_migrations`` bookkeeping table.
- The per-migration transaction boundary
  (``BEGIN; apply; INSERT bookkeeping; COMMIT``).

Individual migrations therefore only own their business tables.

Phase 1 ships ``v001_initial`` — the v2 baseline schema. Later
phases add migrations as new files; previously-shipped migrations
are immutable. See ``docs/PHASE_1_PLAN.md`` §3.4.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2
- ``docs/PHASE_1_PLAN.md`` §3.1, §3.4, §3.5
"""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable


@runtime_checkable
class Migration(Protocol):
    """The protocol the runner expects each migration to satisfy.

    ``id`` is the unique migration identifier (e.g.
    ``"v001_initial"``). It is the value persisted in
    ``applied_migrations.id`` and is the dedup key for re-runs.

    ``description`` is a short human-readable label printed by
    diagnostic tooling. It is not persisted.

    ``apply(conn)`` runs the migration's SQL statements against
    the open connection. The runner has already opened a
    transaction; the migration must NOT call ``COMMIT``,
    ``ROLLBACK``, or anything that issues an implicit commit
    (e.g. ``VACUUM`` or pragma changes that SQLite executes
    outside a TX). Raising any exception triggers a rollback by
    the runner.
    """

    id: str
    description: str

    def apply(self, conn: sqlite3.Connection) -> None: ...


__all__ = ["Migration"]
