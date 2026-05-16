"""V2 scheduler — schema-only foundation for the new architecture.

See ``docs/CONTRACTS_V2_DESIGN.md`` for the full design contract
and ``docs/PHASE_1_PLAN.md`` for the phase-1 build-out scope.

Phase 1 ships:
- Pydantic enums + models (this subpackage's ``enums`` and
  ``models`` modules).
- SQLite DDL + migration runner (``ddl/`` and
  ``migrations/``).
- Tests (under ``tests/v2/``).

Phase 1 does NOT ship runtime code. No worker, no wakeup callback,
no ADK tools. Those land starting at phase 4 (per
``docs/CONTRACTS_V2_DESIGN.md`` §12).
"""
