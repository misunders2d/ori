"""Tests for ``app.v2.authoring.drafts``.

Phase 7 slice 1 per ``docs/PHASE_7_PLAN.md`` §5.2.

Pins:
- DraftStore round-trips, missing file raises
  FileNotFoundError, delete is idempotent, list_ids sorted.
- Atomic write: simulated crash mid-rename preserves the
  previous draft.
- Concurrent same-pid writes do not collide (mirror phase-6
  pin).
- Path-traversal fence (L41 fix): slug regex + Path.resolve
  guard rejects every traversal shape.
- ScheduleSpecDraft.missing_required_fields enumeration.
- to_spec raises when required fields unset; AST pin that
  to_spec body imports no clock (datetime.now / utcnow).
- Round-trip through to_spec + validate_schedule_spec returns
  no issues for a complete draft.
"""

from __future__ import annotations

import ast
import inspect
import os
import textwrap
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.authoring.drafts import (
    DEFAULT_DRAFT_BASE,
    DraftStore,
    ScheduleSpecDraft,
)
from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    ScheduleStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.triggers import OneOffTrigger
from app.v2.validation import validate_schedule_spec


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _full_draft(*, id_: str = "sched_alpha") -> ScheduleSpecDraft:
    """A draft with every required field populated."""
    return ScheduleSpecDraft(
        id=id_,
        description="weekly amazon summary digest",
        owner=UserRef(
            platform="slack",
            user_id="U_OWNER",
            display_name="Sergey",
        ),
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
        status=ScheduleStatus.ACTIVE,
    )


# ===========================================================================
# DEFAULT_DRAFT_BASE
# ===========================================================================


def test_default_draft_base_path():
    assert str(DEFAULT_DRAFT_BASE) == "tmp/v2_drafts"


# ===========================================================================
# Round-trip
# ===========================================================================


def test_write_then_read_round_trip(tmp_path):
    store = DraftStore(base=tmp_path)
    draft = _full_draft()

    store.write("sess1", draft)
    loaded = store.read("sess1", draft.id)

    assert loaded == draft


def test_partial_draft_round_trip(tmp_path):
    """A draft with only ``id`` set still round-trips."""
    store = DraftStore(base=tmp_path)
    draft = ScheduleSpecDraft(id="incomplete")

    store.write("sess1", draft)
    loaded = store.read("sess1", "incomplete")

    assert loaded.id == "incomplete"
    assert loaded.description is None
    assert loaded.trigger is None


def test_read_missing_raises_file_not_found(tmp_path):
    store = DraftStore(base=tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read("sess1", "absent")


# ===========================================================================
# delete idempotency
# ===========================================================================


def test_delete_existing_removes_file(tmp_path):
    store = DraftStore(base=tmp_path)
    store.write("sess1", _full_draft())

    store.delete("sess1", "sched_alpha")

    with pytest.raises(FileNotFoundError):
        store.read("sess1", "sched_alpha")


def test_delete_missing_is_noop(tmp_path):
    store = DraftStore(base=tmp_path)
    # No write; just delete.
    store.delete("sess1", "absent")  # must not raise


# ===========================================================================
# list_ids
# ===========================================================================


def test_list_ids_empty_session(tmp_path):
    store = DraftStore(base=tmp_path)
    assert store.list_ids("sess1") == []


def test_list_ids_sorted(tmp_path):
    store = DraftStore(base=tmp_path)
    store.write("sess1", _full_draft(id_="zebra"))
    store.write("sess1", _full_draft(id_="apple"))
    store.write("sess1", _full_draft(id_="mango"))

    assert store.list_ids("sess1") == ["apple", "mango", "zebra"]


def test_list_ids_ignores_non_json_files(tmp_path):
    store = DraftStore(base=tmp_path)
    store.write("sess1", _full_draft(id_="alpha"))
    # Drop a stray non-json file.
    (tmp_path / "sess1" / "stray.txt").write_text("x")

    assert store.list_ids("sess1") == ["alpha"]


# ===========================================================================
# Atomic write
# ===========================================================================


def test_successful_write_leaves_no_tmp_artifact(tmp_path):
    store = DraftStore(base=tmp_path)
    store.write("sess1", _full_draft())

    session_dir = tmp_path / "sess1"
    contents = sorted(p.name for p in session_dir.iterdir())
    assert contents == ["sched_alpha.json"]


def test_crash_mid_rename_preserves_previous_draft(tmp_path, monkeypatch):
    store = DraftStore(base=tmp_path)

    # 1. Seed a good draft.
    original = _full_draft(id_="sched_alpha")
    original = original.model_copy(
        update={"description": "original description for digest"}
    )
    store.write("sess1", original)
    good_path = tmp_path / "sess1" / "sched_alpha.json"
    good_bytes = good_path.read_bytes()

    # 2. Monkeypatch rename to raise.
    from app.v2.authoring import drafts as drafts_mod

    def _boom(src, dst):
        assert os.path.exists(src), "tmp file should exist"
        raise OSError("simulated rename failure")

    monkeypatch.setattr(drafts_mod.os, "rename", _boom)

    # 3. Try a second write with new content.
    new_draft = _full_draft(id_="sched_alpha").model_copy(
        update={"description": "REPLACED description shall not land"}
    )
    with pytest.raises(OSError, match="simulated rename failure"):
        store.write("sess1", new_draft)

    # 4. Good file unchanged.
    assert good_path.read_bytes() == good_bytes
    # 5. Tmp file lingers for forensic inspection.
    tmp_files = [
        p
        for p in (tmp_path / "sess1").iterdir()
        if p.name.startswith("sched_alpha.json.tmp.")
    ]
    assert len(tmp_files) == 1


def test_concurrent_same_pid_writes_do_not_collide(tmp_path):
    """Same-pid concurrent writes for the same draft id must
    not clobber tmp files (mirror phase-6 mkstemp pin)."""
    store = DraftStore(base=tmp_path)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def _writer(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            draft = _full_draft(id_="sched_alpha").model_copy(
                update={
                    "description": f"description from thread {i:02d}"
                }
            )
            store.write("sess1", draft)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=(i,)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"concurrent errors: {errors!r}"
    # Target exists + parsable.
    loaded = store.read("sess1", "sched_alpha")
    assert loaded.id == "sched_alpha"
    # No tmp leftovers.
    tmp_files = [
        p
        for p in (tmp_path / "sess1").iterdir()
        if p.name.startswith("sched_alpha.json.tmp.")
    ]
    assert tmp_files == []


# ===========================================================================
# Path-traversal fence (L41)
# ===========================================================================


@pytest.mark.parametrize(
    "session_id",
    [
        "../etc",
        "..\\windows",
        "/absolute",
        "with/slash",
        "with\\backslash",
        "",
        "x" * 129,
        "has space",
        "has.dot",
        "weird;chars",
    ],
)
def test_read_rejects_invalid_session_id(tmp_path, session_id):
    store = DraftStore(base=tmp_path)
    with pytest.raises(ValueError, match="invalid session_id"):
        store.read(session_id, "draft_x")


@pytest.mark.parametrize(
    "draft_id",
    [
        "../a",
        "..\\b",
        "/abs",
        "with/slash",
        "",
        "y" * 129,
    ],
)
def test_write_rejects_invalid_draft_id(tmp_path, draft_id):
    store = DraftStore(base=tmp_path)
    bad = ScheduleSpecDraft(id="ok_id")
    # Force the id field to the bad slug bypassing pydantic
    # so we exercise the store's fence specifically.
    object.__setattr__(bad, "id", draft_id)
    with pytest.raises(ValueError, match="invalid draft_id"):
        store.write("sess1", bad)


def test_delete_rejects_invalid_session_id(tmp_path):
    store = DraftStore(base=tmp_path)
    with pytest.raises(ValueError, match="invalid session_id"):
        store.delete("../escape", "x")


def test_list_ids_rejects_invalid_session_id(tmp_path):
    store = DraftStore(base=tmp_path)
    with pytest.raises(ValueError, match="invalid session_id"):
        store.list_ids("../escape")


def test_symlink_escape_rejected(tmp_path):
    """Build a symlink inside the base that points outside.
    The slug regex would accept the segment, but the
    resolve()-and-is_relative_to guard MUST refuse."""
    store = DraftStore(base=tmp_path)
    # Create an outside dir that the symlink will point at.
    outside = tmp_path.parent / "escape_target"
    outside.mkdir(exist_ok=True)
    try:
        session_link = tmp_path / "bad_session"
        session_link.symlink_to(outside)

        with pytest.raises(ValueError, match="escapes base"):
            store.read("bad_session", "any_draft")
    finally:
        # Clean up the outside dir so we don't leak between runs.
        for p in outside.iterdir():
            p.unlink()
        outside.rmdir()


def test_valid_slug_accepted(tmp_path):
    store = DraftStore(base=tmp_path)
    # All these slugs are within the regex.
    for slug in ["ok", "with-dash", "with_underscore", "abc123", "A" * 128]:
        store.write(slug, _full_draft(id_="ok_id"))
        loaded = store.read(slug, "ok_id")
        assert loaded.id == "ok_id"


# ===========================================================================
# ScheduleSpecDraft.missing_required_fields
# ===========================================================================


def test_missing_required_fields_empty_draft():
    draft = ScheduleSpecDraft(id="abc")
    missing = draft.missing_required_fields()
    assert missing == [
        "description",
        "owner",
        "trigger",
        "delivery",
        "failure",
    ]


def test_missing_required_fields_partial():
    draft = ScheduleSpecDraft(
        id="abc",
        description="weekly digest of amazon orders",
        owner=UserRef(platform="slack", user_id="U1"),
    )
    assert draft.missing_required_fields() == [
        "trigger",
        "delivery",
        "failure",
    ]


def test_missing_required_fields_complete():
    draft = _full_draft()
    assert draft.missing_required_fields() == []


# ===========================================================================
# to_spec
# ===========================================================================


def test_to_spec_raises_when_incomplete():
    draft = ScheduleSpecDraft(id="abc")
    with pytest.raises(ValueError, match="missing required fields"):
        draft.to_spec(clock=_fixed_clock)


def test_to_spec_returns_spec_with_fresh_hash():
    draft = _full_draft()
    spec = draft.to_spec(clock=_fixed_clock)

    assert spec.id == draft.id
    assert spec.description == draft.description
    assert spec.hash != ""  # with_fresh_hash populated it
    # authored_at matches the injected clock.
    assert spec.authored_at.startswith("2026-05-15T12:00:00")


def test_to_spec_uses_injected_clock_only():
    """Pin: ``clock()`` is the sole authority for
    ``authored_at``; a counter-clock observes exactly one call."""
    calls = {"i": 0}

    def _counter_clock():
        calls["i"] += 1
        return _UTC_NOW

    _full_draft().to_spec(clock=_counter_clock)
    assert calls["i"] == 1


def _function_body_calls(fn) -> set[str]:
    """AST helper — collect dotted call names in a fn body
    (mirrors the phase-5 `_function_body_calls` pin in
    `test_runtime_defaults.py`)."""
    source = textwrap.dedent(inspect.getsource(fn))
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
    return calls


def test_to_spec_does_not_call_datetime_now():
    """AST pin — `_defaults.py` is the sole `datetime.now`
    site (phase-5 hard rule 10). `to_spec` is clock-pure."""
    calls = _function_body_calls(ScheduleSpecDraft.to_spec)
    forbidden = {"datetime.now", "datetime.utcnow"}
    leaked = calls & forbidden
    assert not leaked, f"to_spec must be clock-free; got {leaked!r}"


def test_drafts_module_does_not_bind_prod_clock():
    from app.v2.authoring import drafts as drafts_mod

    assert not hasattr(drafts_mod, "prod_clock")


def test_complete_draft_validates_clean():
    """Round-trip pin: a fully populated draft → spec →
    validate_schedule_spec returns no issues."""
    spec = _full_draft().to_spec(clock=_fixed_clock)
    result = validate_schedule_spec(spec)
    assert result.ok, f"unexpected issues: {result.issues!r}"


# ===========================================================================
# Clock tz-aware UTC enforcement (round-1 reviewer slice-1 L145)
# ===========================================================================


def test_to_spec_rejects_naive_clock():
    """Naive datetime (no tzinfo) → ValueError. authored_at
    must be tz-aware UTC."""
    def _naive_clock() -> datetime:
        return datetime(2026, 5, 15, 12, 0)  # no tzinfo

    with pytest.raises(ValueError, match="naive datetime"):
        _full_draft().to_spec(clock=_naive_clock)


def test_to_spec_normalises_non_utc_to_utc():
    """tz-aware non-UTC clock output → spec.authored_at
    carries '+00:00' offset (normalised via astimezone)."""
    five_hours_east = timezone(timedelta(hours=5))

    def _est_clock() -> datetime:
        # 12:00 in +05 == 07:00 UTC.
        return datetime(2026, 5, 15, 12, 0, tzinfo=five_hours_east)

    spec = _full_draft().to_spec(clock=_est_clock)
    # ISO string ends in +00:00 (UTC offset).
    assert spec.authored_at.endswith("+00:00")
    # And the wall-clock value reflects the UTC conversion.
    assert "07:00:00" in spec.authored_at


def test_to_spec_utc_clock_passes_through_unchanged():
    """tz-aware UTC clock → authored_at preserves the wall
    clock unchanged."""
    spec = _full_draft().to_spec(clock=_fixed_clock)
    assert spec.authored_at.startswith("2026-05-15T12:00:00")
    assert spec.authored_at.endswith("+00:00")


# ===========================================================================
# ScheduleSpec superset round-trip (round-1 reviewer slice-1 L74)
# ===========================================================================


def test_draft_round_trips_template_and_parent_hash(tmp_path):
    """Draft mirrors the full ScheduleSpec field set; template
    + parent_hash round-trip through write/read."""
    store = DraftStore(base=tmp_path)
    draft = _full_draft().model_copy(
        update={
            "template": TemplateRef(name="OneOffReminder", version="1"),
            "parent_hash": "deadbeef" * 8,  # 64 hex chars
        }
    )

    store.write("sess1", draft)
    loaded = store.read("sess1", draft.id)

    assert loaded.template == TemplateRef(
        name="OneOffReminder", version="1"
    )
    assert loaded.parent_hash == "deadbeef" * 8


def test_to_spec_passes_through_template_and_parent_hash():
    """to_spec must forward template + parent_hash to the
    resulting ScheduleSpec (per L74 fix)."""
    draft = _full_draft().model_copy(
        update={
            "template": TemplateRef(name="OneOffReminder", version="2"),
            "parent_hash": "f" * 64,
        }
    )

    spec = draft.to_spec(clock=_fixed_clock)

    assert spec.template == TemplateRef(
        name="OneOffReminder", version="2"
    )
    assert spec.parent_hash == "f" * 64


def test_template_and_parent_hash_optional_default_none():
    draft = ScheduleSpecDraft(id="sched_minimal")
    assert draft.template is None
    assert draft.parent_hash is None
