"""Tests for ``app.v2.authoring.compile``.

Phase 7 slice 4 per ``docs/PHASE_7_PLAN.md`` §5.5.

Pins:
- Complete draft compiles to a valid ScheduleSpec; ok
  response carries `spec` dict.
- Incomplete draft → not_ready with missing_fields.
- Validation failure → validation_failed carrying issues.
- validate_schedule_spec called with spec ONLY (no
  execution_plans / no registries kwargs) — Q6 reminder-only.
- compile does NOT write to v2 DB (no insert_schedule
  call from this module).
- compile does NOT delete the draft file.
- discard deletes; idempotent (second call → ok with
  "already absent" hint).
- list returns ids in lexicographic order; empty list shape.
- NO `schedule_draft_commit` symbol in the compile module
  (round-2 reviewer L95 / Q5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.v2.authoring import compile as compile_mod
from app.v2.authoring.compile import (
    schedule_draft_compile,
    schedule_draft_discard,
    schedule_draft_list,
)
from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.triggers import OneOffTrigger


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _store(tmp_path) -> DraftStore:
    return DraftStore(base=tmp_path)


def _complete_draft(id_="sched_alpha") -> ScheduleSpecDraft:
    return ScheduleSpecDraft(
        id=id_,
        description="weekly amazon summary digest",
        owner=UserRef(platform="slack", user_id="U_OWNER"),
        trigger=OneOffTrigger(
            at_iso_datetime=_UTC_NOW,
            timezone="UTC",
        ),
        delivery=Delivery(
            target_session_id="C123",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
    )


# ===========================================================================
# schedule_draft_compile
# ===========================================================================


@pytest.mark.asyncio
async def test_compile_complete_draft_returns_ok_with_spec(tmp_path):
    store = _store(tmp_path)
    store.write("sess1", _complete_draft())

    r = await schedule_draft_compile(
        "sched_alpha",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )

    assert r.status == "ok", f"unexpected: {r!r}"
    assert r.spec is not None
    assert r.spec["id"] == "sched_alpha"
    assert r.spec["hash"]  # populated by with_fresh_hash


@pytest.mark.asyncio
async def test_compile_incomplete_draft_returns_not_ready(tmp_path):
    store = _store(tmp_path)
    draft = ScheduleSpecDraft(
        id="sched_alpha", description="weekly digest"
    )
    store.write("sess1", draft)

    r = await schedule_draft_compile(
        "sched_alpha",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )

    assert r.status == "not_ready"
    assert set(r.missing_fields) == {
        "owner",
        "trigger",
        "delivery",
        "failure",
    }


@pytest.mark.asyncio
async def test_compile_missing_draft_returns_not_found(tmp_path):
    r = await schedule_draft_compile(
        "absent",
        session_id="sess1",
        store=_store(tmp_path),
        clock=_fixed_clock,
    )
    assert r.status == "not_found"


@pytest.mark.asyncio
async def test_compile_naive_clock_returns_validation_failed(tmp_path):
    """to_spec rejects naive clocks (slice-1 L145 fix). The
    compile tool surfaces that as a validation_failed shape so
    the LLM doesn't see an exception."""
    store = _store(tmp_path)
    store.write("sess1", _complete_draft())

    def _naive_clock() -> datetime:
        return datetime(2026, 5, 15, 12, 0)  # no tzinfo

    r = await schedule_draft_compile(
        "sched_alpha",
        session_id="sess1",
        store=store,
        clock=_naive_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "to_spec_failed" for i in r.issues)


@pytest.mark.asyncio
async def test_compile_invalid_spec_returns_validation_failed(tmp_path):
    """A draft whose composed spec fails the validation
    chokepoint (e.g. cron trigger without execution_plan_hash
    per design D5) → validation_failed with the issues."""
    store = _store(tmp_path)
    bad = _complete_draft()
    # Swap to a cron trigger without setting execution_plan_hash
    # → reminder-only rule fires.
    from app.v2.models.triggers import CronTrigger

    bad = bad.model_copy(
        update={
            "trigger": CronTrigger(cron="0 9 * * MON", timezone="UTC"),
        }
    )
    store.write("sess1", bad)

    r = await schedule_draft_compile(
        "sched_alpha",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "missing_execution_plan_for_complex_trigger"
        for i in r.issues
    )


@pytest.mark.asyncio
async def test_compile_calls_validate_with_spec_only_no_kwargs(
    tmp_path, monkeypatch
):
    """Q6 pin: validate_schedule_spec is called with spec
    positional and zero kwargs in the reminder-only flow."""
    store = _store(tmp_path)
    store.write("sess1", _complete_draft())

    spy = MagicMock(wraps=compile_mod.validate_schedule_spec)
    monkeypatch.setattr(compile_mod, "validate_schedule_spec", spy)

    await schedule_draft_compile(
        "sched_alpha",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )

    assert spy.call_count == 1
    args, kwargs = spy.call_args
    assert len(args) == 1
    assert kwargs == {}


@pytest.mark.asyncio
async def test_compile_does_not_delete_draft_file(tmp_path):
    """Compile is non-destructive; the draft file stays on
    disk after the call (phase 8's freeze + commit will
    handle disposal)."""
    store = _store(tmp_path)
    store.write("sess1", _complete_draft())

    await schedule_draft_compile(
        "sched_alpha",
        session_id="sess1",
        store=store,
        clock=_fixed_clock,
    )

    # Still loadable.
    assert store.read("sess1", "sched_alpha").id == "sched_alpha"


def test_compile_module_does_not_import_storage_insert():
    """Compile must NOT call ``insert_schedule`` (round-2 L95
    / Q5). Pin via grep — the module's source contains no
    ``insert_schedule`` reference."""
    import inspect

    src = inspect.getsource(compile_mod)
    assert "insert_schedule" not in src


def test_no_schedule_draft_commit_in_compile_module():
    """Phase 7 ships compile + discard + list ONLY (round-2
    reviewer L95 / Q5 — commit deferred to phase 8). Pin so
    a future re-introduction is a deliberate decision."""
    assert not hasattr(compile_mod, "schedule_draft_commit")


# ===========================================================================
# schedule_draft_discard
# ===========================================================================


@pytest.mark.asyncio
async def test_discard_existing_draft(tmp_path):
    store = _store(tmp_path)
    store.write("sess1", _complete_draft())

    r = await schedule_draft_discard(
        "sched_alpha",
        session_id="sess1",
        store=store,
    )

    assert r.status == "ok"
    assert r.draft_id == "sched_alpha"
    # Subsequent read raises.
    import pytest as _pytest

    with _pytest.raises(FileNotFoundError):
        store.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_discard_idempotent_second_call(tmp_path):
    """Second call after a successful discard → ok with
    'already absent' hint in message."""
    store = _store(tmp_path)
    store.write("sess1", _complete_draft())

    await schedule_draft_discard(
        "sched_alpha", session_id="sess1", store=store
    )
    r = await schedule_draft_discard(
        "sched_alpha", session_id="sess1", store=store
    )

    assert r.status == "ok"
    assert r.message is not None
    assert "already absent" in r.message


@pytest.mark.asyncio
async def test_discard_never_existing_draft(tmp_path):
    """Even a draft that never existed → ok with 'already
    absent' hint."""
    store = _store(tmp_path)
    r = await schedule_draft_discard(
        "never_existed", session_id="sess1", store=store
    )

    assert r.status == "ok"
    assert "already absent" in r.message


# ===========================================================================
# schedule_draft_list
# ===========================================================================


@pytest.mark.asyncio
async def test_list_empty_session(tmp_path):
    r = await schedule_draft_list(
        "sess1", store=_store(tmp_path)
    )

    assert r.status == "ok"
    assert r.spec == {"draft_ids": []}


@pytest.mark.asyncio
async def test_list_returns_sorted_ids(tmp_path):
    store = _store(tmp_path)
    store.write("sess1", _complete_draft(id_="zebra"))
    store.write("sess1", _complete_draft(id_="apple"))
    store.write("sess1", _complete_draft(id_="mango"))

    r = await schedule_draft_list("sess1", store=store)

    assert r.status == "ok"
    assert r.spec == {"draft_ids": ["apple", "mango", "zebra"]}


@pytest.mark.asyncio
async def test_list_ignores_other_sessions(tmp_path):
    store = _store(tmp_path)
    store.write("sess1", _complete_draft(id_="alpha"))
    store.write("sess2", _complete_draft(id_="beta"))

    r = await schedule_draft_list("sess1", store=store)

    assert r.status == "ok"
    assert r.spec == {"draft_ids": ["alpha"]}
