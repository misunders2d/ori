"""Tests for ``app.v2.authoring.handshake``.

Phase 8 slice 1 per ``docs/PHASE_8_PLAN.md`` §5.1.

Pins:
- ``DryRunMode`` exposes the three documented values.
- ``HandshakeRecord`` rejects naive + non-UTC datetimes on
  every datetime field (recorded_at, expires_at,
  as_of_datetime); UTC accepted + round-trips.
- ``HandshakeRecord.is_expired`` boundary semantics.
- ``HandshakeStore`` round-trip, missing read raises
  FileNotFoundError, delete idempotent.
- Path-traversal fence: every shape the DraftStore pins
  (``../``, ``..\\``, absolute, slash, empty, oversize,
  symlink escape) raises ValueError BEFORE I/O.
- Atomic write: simulated rename crash preserves previous
  record + leaves a ``.tmp.*`` artifact.
- Concurrent same-pid writes (8 threads) do not collide; no
  tmp leftovers after.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.authoring.handshake import (
    DEFAULT_HANDSHAKE_BASE,
    DryRunMode,
    HandshakeRecord,
    HandshakeStore,
    _HANDSHAKE_WINDOW_SECONDS,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


def _fresh_record(
    *,
    draft_id: str = "sched_alpha",
    session_id: str = "sess1",
    body_hash: str = "a" * 64,
    mode: DryRunMode = DryRunMode.VALIDATE_ONLY,
    as_of: datetime | None = None,
) -> HandshakeRecord:
    return HandshakeRecord(
        draft_id=draft_id,
        session_id=session_id,
        body_hash=body_hash,
        mode=mode,
        as_of_datetime=as_of,
        recorded_at=_UTC_NOW,
        expires_at=_UTC_NOW + timedelta(seconds=_HANDSHAKE_WINDOW_SECONDS),
    )


# ===========================================================================
# Constants
# ===========================================================================


def test_default_handshake_base_path():
    assert str(DEFAULT_HANDSHAKE_BASE) == "tmp/v2_handshakes"


def test_handshake_window_is_60_seconds():
    assert _HANDSHAKE_WINDOW_SECONDS == 60


# ===========================================================================
# DryRunMode
# ===========================================================================


def test_dry_run_mode_has_three_values():
    assert {m.value for m in DryRunMode} == {
        "validate_only",
        "mocked_inputs",
        "real",
    }


def test_dry_run_mode_is_str_enum():
    # str subclass so JSON round-trips via the string value.
    assert DryRunMode.VALIDATE_ONLY == "validate_only"
    assert DryRunMode.MOCKED_INPUTS == "mocked_inputs"
    assert DryRunMode.REAL == "real"


# ===========================================================================
# HandshakeRecord — UTC enforcement
# ===========================================================================


def test_record_accepts_utc_datetimes_round_trip():
    record = _fresh_record(as_of=_UTC_NOW - timedelta(hours=1))
    blob = record.model_dump_json()
    loaded = HandshakeRecord.model_validate_json(blob)
    assert loaded == record


def test_record_rejects_naive_recorded_at():
    with pytest.raises(ValueError, match="naive datetime"):
        HandshakeRecord(
            draft_id="sched_alpha",
            session_id="sess1",
            body_hash="a" * 64,
            mode=DryRunMode.VALIDATE_ONLY,
            recorded_at=datetime(2026, 5, 15, 12, 0),  # no tzinfo
            expires_at=_UTC_NOW + timedelta(seconds=60),
        )


def test_record_rejects_naive_expires_at():
    with pytest.raises(ValueError, match="naive datetime"):
        HandshakeRecord(
            draft_id="sched_alpha",
            session_id="sess1",
            body_hash="a" * 64,
            mode=DryRunMode.VALIDATE_ONLY,
            recorded_at=_UTC_NOW,
            expires_at=datetime(2026, 5, 15, 12, 1),  # no tzinfo
        )


def test_record_rejects_naive_as_of_datetime():
    with pytest.raises(ValueError, match="naive datetime"):
        HandshakeRecord(
            draft_id="sched_alpha",
            session_id="sess1",
            body_hash="a" * 64,
            mode=DryRunMode.VALIDATE_ONLY,
            as_of_datetime=datetime(2026, 5, 15, 11, 0),  # no tzinfo
            recorded_at=_UTC_NOW,
            expires_at=_UTC_NOW + timedelta(seconds=60),
        )


def test_record_rejects_non_utc_recorded_at():
    five_hours_east = timezone(timedelta(hours=5))
    with pytest.raises(ValueError, match="must be UTC"):
        HandshakeRecord(
            draft_id="sched_alpha",
            session_id="sess1",
            body_hash="a" * 64,
            mode=DryRunMode.VALIDATE_ONLY,
            recorded_at=datetime(
                2026, 5, 15, 12, 0, tzinfo=five_hours_east
            ),
            expires_at=_UTC_NOW + timedelta(seconds=60),
        )


def test_record_rejects_non_utc_expires_at():
    five_hours_east = timezone(timedelta(hours=5))
    with pytest.raises(ValueError, match="must be UTC"):
        HandshakeRecord(
            draft_id="sched_alpha",
            session_id="sess1",
            body_hash="a" * 64,
            mode=DryRunMode.VALIDATE_ONLY,
            recorded_at=_UTC_NOW,
            expires_at=datetime(
                2026, 5, 15, 12, 1, tzinfo=five_hours_east
            ),
        )


def test_record_rejects_non_utc_as_of_datetime():
    five_hours_east = timezone(timedelta(hours=5))
    with pytest.raises(ValueError, match="must be UTC"):
        HandshakeRecord(
            draft_id="sched_alpha",
            session_id="sess1",
            body_hash="a" * 64,
            mode=DryRunMode.VALIDATE_ONLY,
            as_of_datetime=datetime(
                2026, 5, 15, 11, 0, tzinfo=five_hours_east
            ),
            recorded_at=_UTC_NOW,
            expires_at=_UTC_NOW + timedelta(seconds=60),
        )


def test_record_accepts_none_as_of_datetime():
    record = _fresh_record(as_of=None)
    assert record.as_of_datetime is None


def test_record_forbids_extra_fields():
    with pytest.raises(ValueError):
        HandshakeRecord(
            draft_id="sched_alpha",
            session_id="sess1",
            body_hash="a" * 64,
            mode=DryRunMode.VALIDATE_ONLY,
            recorded_at=_UTC_NOW,
            expires_at=_UTC_NOW + timedelta(seconds=60),
            stray_field="nope",  # type: ignore[call-arg]
        )


# ===========================================================================
# HandshakeRecord.is_expired boundary
# ===========================================================================


def test_is_expired_false_before_boundary():
    record = _fresh_record()
    now = record.expires_at - timedelta(seconds=1)
    assert record.is_expired(now=now) is False


def test_is_expired_false_at_exact_boundary():
    """``now == expires_at`` → still fresh (strict ``>``)."""
    record = _fresh_record()
    assert record.is_expired(now=record.expires_at) is False


def test_is_expired_true_past_boundary():
    record = _fresh_record()
    now = record.expires_at + timedelta(microseconds=1)
    assert record.is_expired(now=now) is True


def test_is_expired_true_well_past_boundary():
    record = _fresh_record()
    now = record.expires_at + timedelta(hours=24)
    assert record.is_expired(now=now) is True


# ===========================================================================
# HandshakeStore round-trip
# ===========================================================================


def test_write_then_read_round_trip(tmp_path):
    store = HandshakeStore(base=tmp_path)
    record = _fresh_record()

    store.write("sess1", record)
    loaded = store.read("sess1", record.draft_id)

    assert loaded == record


def test_read_missing_raises_file_not_found(tmp_path):
    store = HandshakeStore(base=tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read("sess1", "absent")


def test_round_trip_preserves_as_of_datetime(tmp_path):
    store = HandshakeStore(base=tmp_path)
    record = _fresh_record(as_of=_UTC_NOW - timedelta(hours=2))

    store.write("sess1", record)
    loaded = store.read("sess1", record.draft_id)

    assert loaded.as_of_datetime == _UTC_NOW - timedelta(hours=2)


# ===========================================================================
# HandshakeStore.delete idempotency
# ===========================================================================


def test_delete_existing_removes_file(tmp_path):
    store = HandshakeStore(base=tmp_path)
    store.write("sess1", _fresh_record())

    store.delete("sess1", "sched_alpha")

    with pytest.raises(FileNotFoundError):
        store.read("sess1", "sched_alpha")


def test_delete_missing_is_noop(tmp_path):
    store = HandshakeStore(base=tmp_path)
    store.delete("sess1", "absent")  # must not raise


# ===========================================================================
# Atomic write
# ===========================================================================


def test_successful_write_leaves_no_tmp_artifact(tmp_path):
    store = HandshakeStore(base=tmp_path)
    store.write("sess1", _fresh_record())

    session_dir = tmp_path / "sess1"
    contents = sorted(p.name for p in session_dir.iterdir())
    assert contents == ["sched_alpha.json"]


def test_crash_mid_rename_preserves_previous_record(tmp_path, monkeypatch):
    store = HandshakeStore(base=tmp_path)

    # 1. Seed a good record.
    original = _fresh_record(body_hash="original_hash" + "0" * 51)
    store.write("sess1", original)
    good_path = tmp_path / "sess1" / "sched_alpha.json"
    good_bytes = good_path.read_bytes()

    # 2. Monkeypatch rename to raise.
    from app.v2.authoring import handshake as handshake_mod

    def _boom(src, dst):
        assert os.path.exists(src), "tmp file should exist"
        raise OSError("simulated rename failure")

    monkeypatch.setattr(handshake_mod.os, "rename", _boom)

    # 3. Try a second write with new content.
    replacement = _fresh_record(body_hash="replaced_hash" + "0" * 51)
    with pytest.raises(OSError, match="simulated rename failure"):
        store.write("sess1", replacement)

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
    not clobber tmp files (mirror DraftStore mkstemp pin)."""
    store = HandshakeStore(base=tmp_path)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def _writer(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            record = _fresh_record(
                body_hash=f"thread_{i:02d}" + "0" * (64 - len(f"thread_{i:02d}"))
            )
            store.write("sess1", record)
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
    assert loaded.draft_id == "sched_alpha"
    # No tmp leftovers.
    tmp_files = [
        p
        for p in (tmp_path / "sess1").iterdir()
        if p.name.startswith("sched_alpha.json.tmp.")
    ]
    assert tmp_files == []


# ===========================================================================
# Path-traversal fence
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
    store = HandshakeStore(base=tmp_path)
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
    store = HandshakeStore(base=tmp_path)
    bad = _fresh_record()
    # Force the draft_id field to the bad slug bypassing
    # pydantic so we exercise the store's fence specifically.
    object.__setattr__(bad, "draft_id", draft_id)
    with pytest.raises(ValueError, match="invalid draft_id"):
        store.write("sess1", bad)


def test_delete_rejects_invalid_session_id(tmp_path):
    store = HandshakeStore(base=tmp_path)
    with pytest.raises(ValueError, match="invalid session_id"):
        store.delete("../escape", "x")


def test_symlink_escape_rejected(tmp_path):
    """Build a symlink inside the base that points outside.
    The slug regex would accept the segment, but the
    resolve()-and-is_relative_to guard MUST refuse."""
    store = HandshakeStore(base=tmp_path)
    outside = tmp_path.parent / "handshake_escape_target"
    outside.mkdir(exist_ok=True)
    try:
        session_link = tmp_path / "bad_session"
        session_link.symlink_to(outside)

        with pytest.raises(ValueError, match="escapes base"):
            store.read("bad_session", "any_draft")
    finally:
        for p in outside.iterdir():
            p.unlink()
        outside.rmdir()


def test_valid_slug_accepted(tmp_path):
    store = HandshakeStore(base=tmp_path)
    for slug in ["ok", "with-dash", "with_underscore", "abc123", "A" * 128]:
        record = _fresh_record(session_id=slug)
        store.write(slug, record)
        loaded = store.read(slug, "sched_alpha")
        assert loaded.session_id == slug


# ===========================================================================
# Default-base constructor
# ===========================================================================


def test_default_base_used_when_none_passed():
    store = HandshakeStore()
    assert store.base == DEFAULT_HANDSHAKE_BASE
