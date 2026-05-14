-- v001_initial: scheduler v2 baseline schema.
--
-- See docs/CONTRACTS_V2_DESIGN.md §4.0.2 for canonical spec.
-- Forward-only; never edit after ship. New changes are new files.
--
-- This file owns ONLY the v2 business tables + their indexes.
-- The runner (app/v2/migrations/runner.py) owns:
--   - the `applied_migrations` bookkeeping table
--   - PRAGMA journal_mode=WAL (set outside any TX)
--   - PRAGMA foreign_keys=ON (per-connection)


-- ScheduleSpec: the root row for a recurring or one-off task.
CREATE TABLE schedules (
    id                  TEXT PRIMARY KEY,
    owner               TEXT NOT NULL,
    description         TEXT NOT NULL,
    trigger_json        TEXT NOT NULL,
    delivery_json       TEXT NOT NULL,
    failure_json        TEXT NOT NULL,
    audit_json          TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('active', 'paused', 'archived')),
    execution_plan_hash TEXT,
    template_json       TEXT,
    authored_at         TEXT NOT NULL,
    parent_hash         TEXT,
    hash                TEXT NOT NULL UNIQUE
);

CREATE INDEX idx_schedules_status ON schedules(status);
CREATE INDEX idx_schedules_owner ON schedules(owner);
CREATE INDEX idx_schedules_plan_hash ON schedules(execution_plan_hash);


-- ExecutionPlan: immutable, hash-addressed workflow body.
-- INSERT-only; never UPDATE.
CREATE TABLE execution_plans (
    hash        TEXT PRIMARY KEY,
    body_json   TEXT NOT NULL,
    enforcement TEXT NOT NULL CHECK (enforcement IN ('strict', 'permissive')),
    authored_at TEXT NOT NULL,
    author      TEXT NOT NULL
);


-- Run: one execution attempt. Failed runs are terminal; retries
-- are NEW rows with parent_run_id + root_run_id pointing back.
CREATE TABLE runs (
    id                  TEXT PRIMARY KEY,
    schedule_id         TEXT NOT NULL,
    execution_plan_hash TEXT,
    fire_reason         TEXT NOT NULL CHECK (fire_reason IN ('scheduled', 'manual', 'retry', 'replay', 'backfill')),
    due_at              TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt             INTEGER NOT NULL DEFAULT 1,
    root_run_id         TEXT NOT NULL,
    parent_run_id       TEXT,
    claimed_by          TEXT,
    claimed_at          TEXT,
    started_at          TEXT,
    completed_at        TEXT,
    error               TEXT,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id),
    FOREIGN KEY (execution_plan_hash) REFERENCES execution_plans(hash),
    FOREIGN KEY (root_run_id) REFERENCES runs(id),
    FOREIGN KEY (parent_run_id) REFERENCES runs(id)
);

CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_due_at ON runs(due_at);
CREATE INDEX idx_runs_schedule_due ON runs(schedule_id, due_at);
CREATE INDEX idx_runs_pending_due ON runs(status, due_at) WHERE status = 'pending';
CREATE INDEX idx_runs_root ON runs(root_run_id);
CREATE INDEX idx_runs_schedule_running ON runs(schedule_id, status) WHERE status IN ('claimed', 'running');


-- EventLedger: append-only audit truth. Every state transition
-- and every emit produces one row.
-- kind is constrained to the canonical EventKind set
-- (app/v2/enums.py:EventKind). New kinds require a new migration
-- to widen the CHECK alongside the enum addition.
CREATE TABLE events (
    id           TEXT PRIMARY KEY,
    run_id       TEXT,
    schedule_id  TEXT NOT NULL,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN (
                     'schedule_created', 'schedule_revised', 'schedule_paused',
                     'schedule_archived', 'schedule_resumed', 'schedule_revived',
                     'run_created', 'run_claimed', 'run_started', 'run_recovered',
                     'run_succeeded', 'run_failed', 'run_retry_scheduled', 'run_cancelled',
                     'source_resolved', 'source_drift_detected', 'source_failed',
                     'reasoning_started', 'reasoning_completed', 'reasoning_failed',
                     'emit_started', 'emit_succeeded', 'emit_failed', 'emit_skipped_idempotent',
                     'delivery_failed', 'admin_alert_sent', 'admin_alert_acked',
                     'audit_mirror_appended', 'boot_self_test_passed',
                     'boot_self_test_failed', 'migration_v1_to_v2_complete'
                 )),
    payload_json TEXT NOT NULL,
    correlates   TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id),
    FOREIGN KEY (schedule_id) REFERENCES schedules(id)
);

CREATE INDEX idx_events_run ON events(run_id);
CREATE INDEX idx_events_schedule_ts ON events(schedule_id, ts);
CREATE INDEX idx_events_kind ON events(kind);


-- Cross-fire schedule state (per-schedule key/value with version).
-- written_by_run is nullable: author-time seeds have no run to
-- attribute. When set, it must reference a real Run for replay
-- + lineage queries (no orphaned attributions).
CREATE TABLE schedule_state (
    schedule_id    TEXT NOT NULL,
    key            TEXT NOT NULL,
    value_json     TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    written_at     TEXT NOT NULL,
    written_by_run TEXT,
    PRIMARY KEY (schedule_id, key),
    FOREIGN KEY (schedule_id) REFERENCES schedules(id),
    FOREIGN KEY (written_by_run) REFERENCES runs(id)
);


-- Source snapshots: filesystem stores the bytes (keyed by hash);
-- SQLite indexes the metadata.
-- selection_method records HOW the fire-time item was picked so
-- the audit log can explain "why item 5 instead of item 12".
-- Constrained to the SelectionMethod enum
-- (app/v2/enums.py:SelectionMethod).
CREATE TABLE source_snapshots (
    run_id           TEXT NOT NULL,
    source_id        TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    content_path     TEXT NOT NULL,
    content_size     INTEGER NOT NULL,
    fetched_at       TEXT NOT NULL,
    source_kind      TEXT NOT NULL,
    source_version   TEXT,
    selection_method TEXT NOT NULL CHECK (selection_method IN (
                         'stable_id', 'content_hash', 'row_number'
                     )),
    PRIMARY KEY (run_id, source_id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);

CREATE INDEX idx_snapshots_hash ON source_snapshots(content_hash);
