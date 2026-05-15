"""Tests for ``app.v2.authoring.dry_run``.

Phase 8 slice 2 per ``docs/PHASE_8_PLAN.md`` §5.2.

Pins:
- validate_only happy path: ok carries spec; handshake
  written with body_hash == spec.hash, mode VALIDATE_ONLY,
  expires_at == recorded_at + 60s.
- validate_only on incomplete draft → not_ready; no
  handshake written.
- validate_only on failing spec (cron without plan) →
  validation_failed; no handshake written.
- Missing draft → not_found.
- mocked_inputs → validation_failed code
  mode_not_implemented_in_phase_8; no handshake; no draft
  read attempted (mode gate runs first).
- real → same shape as mocked_inputs.
- Naive clock → to_spec_failed validation_failed; no
  handshake.
- as_of_datetime accepted in validate_only and stored
  verbatim on the record.
- validate_schedule_spec called with spec positional + zero
  kwargs (Q6 carry-forward pin).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.v2.authoring import dry_run as dry_run_mod
from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.authoring.dry_run import schedule_dry_run
from app.v2.authoring.handshake import (
    _HANDSHAKE_WINDOW_SECONDS,
    DryRunMode,
    HandshakeStore,
)
from app.v2.enums import DeliveryFallbackPolicy, FailureActionType
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.triggers import CronTrigger, OneOffTrigger


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _stores(tmp_path) -> tuple[DraftStore, HandshakeStore]:
    return (
        DraftStore(base=tmp_path / "drafts"),
        HandshakeStore(base=tmp_path / "handshakes"),
    )


def _complete_draft(id_="sched_alpha") -> ScheduleSpecDraft:
    return ScheduleSpecDraft(
        id=id_,
        description="weekly amazon summary digest",
        owner=UserRef(platform="slack", user_id="U_OWNER"),
        trigger=OneOffTrigger(
            at_iso_datetime=_UTC_NOW + timedelta(days=1),
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
# validate_only — happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_validate_only_happy_path_writes_handshake(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "ok", f"unexpected: {r!r}"
    assert r.spec is not None
    assert r.spec["id"] == "sched_alpha"
    spec_hash = r.spec["hash"]
    assert spec_hash

    # Handshake landed.
    record = handshakes.read("sess1", "sched_alpha")
    assert record.draft_id == "sched_alpha"
    assert record.session_id == "sess1"
    assert record.body_hash == spec_hash
    assert record.mode is DryRunMode.VALIDATE_ONLY
    assert record.recorded_at == _UTC_NOW
    assert record.expires_at == _UTC_NOW + timedelta(
        seconds=_HANDSHAKE_WINDOW_SECONDS
    )
    assert record.as_of_datetime is None


@pytest.mark.asyncio
async def test_validate_only_ok_message_names_freeze_window(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    assert r.message is not None
    assert "60s" in r.message
    assert "freeze" in r.message


# ===========================================================================
# validate_only — not_ready / validation_failed / not_found
# ===========================================================================


@pytest.mark.asyncio
async def test_validate_only_incomplete_draft_returns_not_ready(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    draft = ScheduleSpecDraft(
        id="sched_alpha", description="weekly digest"
    )
    drafts.write("sess1", draft)

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "not_ready"
    assert set(r.missing_fields) == {
        "owner",
        "trigger",
        "delivery",
        "failure",
    }
    # No handshake written.
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_validate_only_failing_spec_returns_validation_failed(tmp_path):
    """Cron trigger without execution_plan_hash trips the
    reminder-only rule (design D5). The tool surfaces issues
    and does NOT write a handshake."""
    drafts, handshakes = _stores(tmp_path)
    bad = _complete_draft().model_copy(
        update={
            "trigger": CronTrigger(cron="0 9 * * MON", timezone="UTC"),
        }
    )
    drafts.write("sess1", bad)

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "missing_execution_plan_for_complex_trigger"
        for i in r.issues
    )
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_missing_draft_returns_not_found(tmp_path):
    drafts, handshakes = _stores(tmp_path)

    r = await schedule_dry_run(
        "absent",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "not_found"
    assert "absent" in r.message


@pytest.mark.asyncio
async def test_validate_only_naive_clock_returns_validation_failed(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    def _naive_clock() -> datetime:
        return datetime(2026, 5, 15, 12, 0)  # no tzinfo

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_naive_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "to_spec_failed" for i in r.issues)
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


# ===========================================================================
# mocked_inputs / real — stubbed modes
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [DryRunMode.MOCKED_INPUTS, DryRunMode.REAL]
)
async def test_stubbed_modes_return_mode_not_implemented(tmp_path, mode):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    r = await schedule_dry_run(
        "sched_alpha",
        mode,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "mode_not_implemented_in_phase_8" for i in r.issues
    )
    assert any(
        mode.value in i.message for i in r.issues
    )
    # No handshake written.
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [DryRunMode.MOCKED_INPUTS, DryRunMode.REAL]
)
async def test_stubbed_modes_do_not_read_draft(
    tmp_path, monkeypatch, mode
):
    """Mode gate runs BEFORE any I/O. Pin via a DraftStore
    whose read raises if invoked."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    sentinel = RuntimeError("DraftStore.read must not be called")

    def _boom(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr(drafts, "read", _boom)

    r = await schedule_dry_run(
        "sched_alpha",
        mode,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "mode_not_implemented_in_phase_8" for i in r.issues
    )


# ===========================================================================
# as_of_datetime storage
# ===========================================================================


@pytest.mark.asyncio
async def test_as_of_datetime_stored_verbatim_on_validate_only(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    as_of = _UTC_NOW - timedelta(hours=3)

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
        as_of_datetime=as_of,
    )
    assert r.status == "ok"

    record = handshakes.read("sess1", "sched_alpha")
    assert record.as_of_datetime == as_of


@pytest.mark.asyncio
async def test_as_of_datetime_default_none(tmp_path):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    record = handshakes.read("sess1", "sched_alpha")
    assert record.as_of_datetime is None


# ===========================================================================
# validate_schedule_spec call shape (Q6)
# ===========================================================================


@pytest.mark.asyncio
async def test_calls_validate_with_spec_only_no_kwargs(
    tmp_path, monkeypatch
):
    """Q6 carry-forward pin: reminder-only flow passes spec
    positional with zero kwargs."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    spy = MagicMock(wraps=dry_run_mod.validate_schedule_spec)
    monkeypatch.setattr(dry_run_mod, "validate_schedule_spec", spy)

    await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert spy.call_count == 1
    args, kwargs = spy.call_args
    assert len(args) == 1
    assert kwargs == {}


# ===========================================================================
# Non-UTC clock normalisation
# ===========================================================================


@pytest.mark.asyncio
async def test_non_utc_clock_normalised_to_utc_on_record(tmp_path):
    """Phase-5 invariant: tz-aware non-UTC clock outputs are
    normalised to UTC before persistence. The handshake
    record's recorded_at carries utcoffset==0."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    five_east = timezone(timedelta(hours=5))

    def _est_clock() -> datetime:
        # 12:00 +05 == 07:00 UTC.
        return datetime(2026, 5, 15, 12, 0, tzinfo=five_east)

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_est_clock,
    )
    assert r.status == "ok"

    record = handshakes.read("sess1", "sched_alpha")
    assert record.recorded_at == datetime(
        2026, 5, 15, 7, 0, tzinfo=timezone.utc
    )
    assert record.expires_at == record.recorded_at + timedelta(seconds=60)


# ===========================================================================
# String-mode coverage (slice-2 fix-up — reviewer bug)
# ===========================================================================


@pytest.mark.asyncio
async def test_string_mode_validate_only_happy_path(tmp_path):
    """LLM may pass the raw string 'validate_only' rather
    than the enum. Coercion accepts it."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    r = await schedule_dry_run(
        "sched_alpha",
        "validate_only",  # raw string
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "ok"
    record = handshakes.read("sess1", "sched_alpha")
    assert record.mode is DryRunMode.VALIDATE_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode_str", ["mocked_inputs", "real"]
)
async def test_string_mode_stubbed_modes_refused(tmp_path, mode_str):
    """The reviewer bug: raw string 'real' / 'mocked_inputs'
    must hit the mode_not_implemented_in_phase_8 gate before
    any draft read. Pin via monkeypatched read."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    def _boom(*args, **kwargs):
        raise RuntimeError("DraftStore.read must not be called")

    object.__setattr__(drafts, "read", _boom)

    r = await schedule_dry_run(
        "sched_alpha",
        mode_str,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(
        i.code == "mode_not_implemented_in_phase_8" for i in r.issues
    )
    assert any(mode_str in i.message for i in r.issues)
    # No handshake written.
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_mode",
    [
        "garbage",
        "validateonly",  # close but wrong
        "VALIDATE_ONLY",  # case sensitive
        "",
        42,
        None,
    ],
)
async def test_unknown_mode_returns_invalid_dry_run_mode(tmp_path, bad_mode):
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    # Also pin that the gate runs before any I/O.
    def _boom(*args, **kwargs):
        raise RuntimeError("DraftStore.read must not be called")

    object.__setattr__(drafts, "read", _boom)

    r = await schedule_dry_run(
        "sched_alpha",
        bad_mode,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
    )

    assert r.status == "validation_failed"
    assert any(i.code == "invalid_dry_run_mode" for i in r.issues)
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


# ===========================================================================
# as_of_datetime UTC enforcement (slice-2 fix-up — reviewer risk)
# ===========================================================================


@pytest.mark.asyncio
async def test_as_of_datetime_naive_returns_validation_failed(tmp_path):
    """Naive as_of_datetime → validation_failed(as_of_datetime_not_utc).
    Must NOT raise pydantic ValidationError to the caller."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
        as_of_datetime=datetime(2026, 5, 15, 10, 0),  # naive
    )

    assert r.status == "validation_failed"
    assert any(i.code == "as_of_datetime_not_utc" for i in r.issues)
    assert any("naive" in i.message for i in r.issues)
    # No handshake written.
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_as_of_datetime_non_utc_returns_validation_failed(tmp_path):
    """Non-UTC tz-aware as_of_datetime →
    validation_failed(as_of_datetime_not_utc) with conversion
    hint. Must NOT raise pydantic ValidationError."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    five_east = timezone(timedelta(hours=5))

    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
        as_of_datetime=datetime(2026, 5, 15, 12, 0, tzinfo=five_east),
    )

    assert r.status == "validation_failed"
    assert any(i.code == "as_of_datetime_not_utc" for i in r.issues)
    assert any("astimezone" in i.message for i in r.issues)
    with pytest.raises(FileNotFoundError):
        handshakes.read("sess1", "sched_alpha")


@pytest.mark.asyncio
async def test_as_of_datetime_utc_passes_through(tmp_path):
    """Sanity pin: UTC as_of_datetime accepted + stored
    verbatim (carries forward the existing pin under new
    guard ordering)."""
    drafts, handshakes = _stores(tmp_path)
    drafts.write("sess1", _complete_draft())

    as_of = _UTC_NOW - timedelta(hours=3)
    r = await schedule_dry_run(
        "sched_alpha",
        DryRunMode.VALIDATE_ONLY,
        session_id="sess1",
        store=drafts,
        handshake_store=handshakes,
        clock=_fixed_clock,
        as_of_datetime=as_of,
    )
    assert r.status == "ok"

    record = handshakes.read("sess1", "sched_alpha")
    assert record.as_of_datetime == as_of


# ===========================================================================
# Module hygiene
# ===========================================================================


def test_dry_run_module_does_not_bind_prod_clock():
    """Phase-5 rule 10: only ``_defaults.py`` may import
    runtime clocks. The authoring module stays clock-pure."""
    assert not hasattr(dry_run_mod, "prod_clock")


def test_dry_run_module_imports_no_datetime_now():
    """AST pin: schedule_dry_run does not call
    ``datetime.now`` / ``datetime.utcnow``."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(schedule_dry_run))
    tree = ast.parse(source)
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            parts: list[str] = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
                calls.add(".".join(reversed(parts)))

    forbidden = {"datetime.now", "datetime.utcnow"}
    leaked = calls & forbidden
    assert not leaked, f"schedule_dry_run must be clock-free; got {leaked!r}"
