"""Tests for ``app.v2.models.snapshot``.

Pins:
- ``content_hash`` matches ``sha256:<64-hex>`` pattern.
- ``content_path`` is relative (no leading ``/``, no ``..``
  segments).
- ``content_size`` is non-negative.
- ``source_version`` is optional.
- ``selection_method`` constrained to the SelectionMethod enum.
- extra fields rejected.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.6.4
- ``docs/PHASE_1_PLAN.md`` §4.9 / §5.1
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.enums import SelectionMethod
from app.v2.models.snapshot import SourceSnapshotMetadata


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
_VALID_HASH = "sha256:" + "a" * 64


def _baseline_kwargs(**overrides):
    base = dict(
        run_id="r0000001-1111-1111-1111-111111111111",
        source_id="syllabus",
        content_hash=_VALID_HASH,
        content_path=(
            "data/contract_audit/linux_mastery/run-abc/sources/syllabus.json"
        ),
        content_size=1234,
        fetched_at=_NOW,
        source_kind="source_drive_file",
        source_version="rev-12345",
        selection_method=SelectionMethod.STABLE_ID,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# content_hash format
# ---------------------------------------------------------------------------


def test_content_hash_accepts_valid_sha256():
    s = SourceSnapshotMetadata(**_baseline_kwargs())
    assert s.content_hash == _VALID_HASH


def test_content_hash_rejects_missing_prefix():
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(**_baseline_kwargs(content_hash="a" * 64))


def test_content_hash_rejects_wrong_algorithm():
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(
            **_baseline_kwargs(content_hash="md5:" + "a" * 32)
        )


def test_content_hash_rejects_short_digest():
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(
            **_baseline_kwargs(content_hash="sha256:" + "a" * 32)
        )


def test_content_hash_rejects_uppercase_hex():
    """SHA-256 hex digests are conventionally lowercase. Pinning
    the case stops two callers producing different keys for the
    same content."""
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(
            **_baseline_kwargs(content_hash="sha256:" + "A" * 64)
        )


def test_content_hash_rejects_non_hex_chars():
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(
            **_baseline_kwargs(content_hash="sha256:" + "z" * 64)
        )


# ---------------------------------------------------------------------------
# content_path safety
# ---------------------------------------------------------------------------


def test_content_path_rejects_absolute():
    with pytest.raises(ValidationError, match="relative"):
        SourceSnapshotMetadata(
            **_baseline_kwargs(content_path="/etc/passwd")
        )


def test_content_path_rejects_parent_traversal():
    with pytest.raises(ValidationError, match="\\.\\."):
        SourceSnapshotMetadata(
            **_baseline_kwargs(content_path="data/../../../etc/passwd")
        )


def test_content_path_accepts_nested_relative():
    s = SourceSnapshotMetadata(
        **_baseline_kwargs(
            content_path=(
                "data/contract_audit/sched/run/sources/source.json"
            )
        )
    )
    assert s.content_path.startswith("data/")


# ---------------------------------------------------------------------------
# Field constraints
# ---------------------------------------------------------------------------


def test_content_size_non_negative():
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(**_baseline_kwargs(content_size=-1))


def test_content_size_zero_allowed():
    """An empty file is a real edge case — Drive doc emptied
    between fires. The snapshot still exists with size 0; the
    runtime decides what to do."""
    s = SourceSnapshotMetadata(**_baseline_kwargs(content_size=0))
    assert s.content_size == 0


def test_source_version_optional():
    kwargs = _baseline_kwargs()
    kwargs.pop("source_version")
    s = SourceSnapshotMetadata(**kwargs)
    assert s.source_version is None


# ---------------------------------------------------------------------------
# selection_method enum
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        SelectionMethod.STABLE_ID,
        SelectionMethod.CONTENT_HASH,
        SelectionMethod.ROW_NUMBER,
    ],
)
def test_selection_method_accepts_canonical_values(method):
    s = SourceSnapshotMetadata(**_baseline_kwargs(selection_method=method))
    assert s.selection_method == method


def test_selection_method_rejects_unknown():
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(
            **_baseline_kwargs(selection_method="first_row")
        )


# ---------------------------------------------------------------------------
# extra-field rejection
# ---------------------------------------------------------------------------


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        SourceSnapshotMetadata(**_baseline_kwargs(checksum="abc"))
