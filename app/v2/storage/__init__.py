"""V2 storage layer — typed CRUD over the v001 SQLite schema.

Slice 1 shipped the foundation: connection contract +
JSON / Pydantic serialization glue. Slice 2 adds the
transaction context manager + the atomic run-update /
event-append helper. Later slices add per-table CRUD modules
on top.

Public re-exports keep the import surface stable across slices.
Callers should import from ``app.v2.storage`` (this package)
rather than the individual modules.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 (schema), §4.0.4 (invariants)
- ``docs/PHASE_3_PLAN.md`` §3 (connection), §4 (transactions),
  §6 (JSON)
"""

from app.v2.storage.connection import (
    ConnectionNotReady,
    assert_connection_ready,
)
from app.v2.storage.serialization import (
    NaiveDatetimeError,
    decode_json,
    encode_json,
)
from app.v2.storage.events import (
    append_event,
    get_last_emit_succeeded,
    list_events_for_run,
    list_events_for_schedule,
)
from app.v2.storage.execution_plans import (
    ExecutionPlanNotFrozenError,
    get_execution_plan,
    insert_execution_plan,
)
from app.v2.storage.runs import (
    ALLOWED_EXTRA_RUN_COLUMNS,
    get_run,
    insert_run,
    list_pending_due,
    list_runs_in_chain,
    mark_run_status,
)
from app.v2.storage.schedule_state import (
    StateRunMismatchError,
    get_state,
    set_state_cas,
)
from app.v2.storage.schedules import (
    ScheduleNotFoundError,
    ScheduleNotFrozenError,
    get_schedule,
    insert_schedule,
    list_active_schedules,
    update_schedule_status,
)
from app.v2.storage.transactions import (
    EventRunMismatchError,
    RunNotFoundError,
    UnknownExtraColumnError,
    transaction,
    update_run_status_and_append_event,
)


__all__ = [
    "ConnectionNotReady",
    "assert_connection_ready",
    "NaiveDatetimeError",
    "decode_json",
    "encode_json",
    "append_event",
    "get_last_emit_succeeded",
    "list_events_for_run",
    "list_events_for_schedule",
    "ExecutionPlanNotFrozenError",
    "get_execution_plan",
    "insert_execution_plan",
    "ALLOWED_EXTRA_RUN_COLUMNS",
    "get_run",
    "insert_run",
    "list_pending_due",
    "list_runs_in_chain",
    "mark_run_status",
    "StateRunMismatchError",
    "get_state",
    "set_state_cas",
    "ScheduleNotFoundError",
    "ScheduleNotFrozenError",
    "get_schedule",
    "insert_schedule",
    "list_active_schedules",
    "update_schedule_status",
    "EventRunMismatchError",
    "RunNotFoundError",
    "UnknownExtraColumnError",
    "transaction",
    "update_run_status_and_append_event",
]
