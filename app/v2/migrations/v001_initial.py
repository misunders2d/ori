"""v001_initial: scheduler v2 baseline schema.

Reads ``app/v2/ddl/v001_initial.sql`` and executes its
``CREATE TABLE`` + ``CREATE INDEX`` statements against the open
SQLite connection.

The runner manages the surrounding transaction + the
``applied_migrations`` bookkeeping row. This module ONLY owns the
v2 business tables.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2
- ``docs/PHASE_1_PLAN.md`` §3.2
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


_SQL_PATH = Path(__file__).resolve().parent.parent / "ddl" / "v001_initial.sql"


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements on the
    semicolon boundary.

    Our migration files use plain DDL — no triggers, no string
    literals containing semicolons. A naive split is therefore
    safe and avoids depending on a SQL parser, with one caveat:
    line comments (``-- ...``) may themselves contain
    semicolons, which would corrupt the split. We strip
    comment-only lines BEFORE splitting to dodge that.
    """
    # Drop pure-comment lines first so semicolons inside comment
    # prose can't confuse the splitter.
    code_lines = [
        ln for ln in sql.splitlines() if not ln.strip().startswith("--")
    ]
    cleaned = "\n".join(code_lines)

    out: list[str] = []
    for chunk in cleaned.split(";"):
        stmt = chunk.strip()
        if stmt:
            out.append(stmt)
    return out


class V001Initial:
    """Baseline migration. Creates ``schedules``,
    ``execution_plans``, ``runs``, ``events``, ``schedule_state``,
    and ``source_snapshots`` plus their indexes.
    """

    id = "v001_initial"
    description = "v2 scheduler baseline schema (6 tables + indexes)"

    def apply(self, conn: sqlite3.Connection) -> None:
        sql = _SQL_PATH.read_text(encoding="utf-8")
        for stmt in _split_statements(sql):
            conn.execute(stmt)


__all__ = ["V001Initial"]
