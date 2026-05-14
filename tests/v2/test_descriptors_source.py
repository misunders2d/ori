"""Tests for ``app.v2.descriptors.source``.

Pins per ``docs/PHASE_2_PLAN.md`` §5.3:

**SourceDescriptor:**
- Requires ``READ_EXTERNAL`` in tags.
- Rejects descriptors carrying ``WRITE_EXTERNAL | SEND_MESSAGE |
  FILESYSTEM_WRITE`` (source layer never writes).
- ``supported_selection_methods`` non-empty.
- Drift guard: each ``SelectionMethod`` value accepted.

**SourceInputContract:**
- Required fields present.
- ``as_of_datetime`` is optional.
- Subclassing supported.

**SourceOutputContract:**
- ``content_hash`` regex matches phase-1 SourceSnapshotMetadata.
- ``content_path`` rejects absolute paths + ``..``.
- ``content_size >= 0``.
- ``selection_method`` constrained to enum.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.descriptors.source import (
    SourceDescriptor,
    SourceInputContract,
    SourceOutputContract,
)
from app.v2.enums import SelectionMethod
from app.v2.tool_tags import ToolCapabilityTag


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
_VALID_HASH = "sha256:" + "a" * 64


# ===========================================================================
# SourceDescriptor
# ===========================================================================


def _descriptor_kwargs(**overrides):
    base = dict(
        id="source_drive_file",
        description="load a Google Drive file by id",
        tags={ToolCapabilityTag.READ_EXTERNAL, ToolCapabilityTag.USES_OAUTH},
        supports_versioning=True,
        supported_selection_methods=[
            SelectionMethod.STABLE_ID,
            SelectionMethod.CONTENT_HASH,
        ],
    )
    base.update(overrides)
    return base


def test_descriptor_constructs_with_canonical_kwargs():
    d = SourceDescriptor(**_descriptor_kwargs())
    assert d.id == "source_drive_file"
    assert d.supports_versioning is True


def test_descriptor_requires_read_external_tag():
    with pytest.raises(ValidationError, match="READ_EXTERNAL"):
        SourceDescriptor(
            **_descriptor_kwargs(
                tags={ToolCapabilityTag.USES_OAUTH},
            )
        )


@pytest.mark.parametrize(
    "forbidden",
    [
        ToolCapabilityTag.WRITE_EXTERNAL,
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    ],
)
def test_descriptor_rejects_write_side_tags(forbidden):
    with pytest.raises(ValidationError, match="write-side"):
        SourceDescriptor(
            **_descriptor_kwargs(
                tags={ToolCapabilityTag.READ_EXTERNAL, forbidden},
            )
        )


def test_descriptor_allows_observational_tags():
    """``costly`` + ``uses_oauth`` are observational, not write-
    side. A source loader may carry them (e.g. a BigQuery scan
    that is both ``read_external`` and ``costly``)."""
    SourceDescriptor(
        **_descriptor_kwargs(
            tags={
                ToolCapabilityTag.READ_EXTERNAL,
                ToolCapabilityTag.COSTLY,
                ToolCapabilityTag.USES_OAUTH,
            },
        )
    )


def test_descriptor_id_must_be_snake_case():
    with pytest.raises(ValidationError, match="snake_case"):
        SourceDescriptor(**_descriptor_kwargs(id="SourceDriveFile"))


def test_descriptor_supported_selection_methods_non_empty():
    with pytest.raises(ValidationError):
        SourceDescriptor(
            **_descriptor_kwargs(supported_selection_methods=[])
        )


def test_descriptor_drift_guard_every_selection_method_accepted():
    for method in SelectionMethod:
        SourceDescriptor(
            **_descriptor_kwargs(supported_selection_methods=[method])
        )


def test_descriptor_extra_field_rejected():
    with pytest.raises(ValidationError):
        SourceDescriptor(**_descriptor_kwargs(rate_limit=10))


# ===========================================================================
# SourceInputContract
# ===========================================================================


def test_input_required_fields():
    s = SourceInputContract(run_id="r1", source_id="syllabus")
    assert s.run_id == "r1"
    assert s.as_of_datetime is None


def test_input_as_of_datetime_accepts_tz_aware():
    s = SourceInputContract(
        run_id="r1", source_id="syllabus", as_of_datetime=_NOW
    )
    assert s.as_of_datetime == _NOW


def test_input_run_id_min_length():
    with pytest.raises(ValidationError):
        SourceInputContract(run_id="", source_id="syllabus")


def test_input_subclassing_supported():
    """Concrete loaders subclass with extra fields. The base
    contract must allow it without overriding ``extra='forbid'``
    on the subclass (Pydantic v2 carries ``extra`` per-class)."""
    from pydantic import ConfigDict

    class DriveSourceInput(SourceInputContract):
        model_config = ConfigDict(extra="forbid")
        drive_file_id: str
        revision: str | None = None

    s = DriveSourceInput(
        run_id="r1",
        source_id="syllabus",
        drive_file_id="1abc",
    )
    assert s.drive_file_id == "1abc"


def test_input_extra_field_rejected():
    with pytest.raises(ValidationError):
        SourceInputContract(
            run_id="r1", source_id="syllabus", drive_file_id="x"
        )


# ===========================================================================
# SourceOutputContract
# ===========================================================================


def _output_kwargs(**overrides):
    base = dict(
        content_hash=_VALID_HASH,
        content_path="data/sources/run-1/syllabus.json",
        content_size=1234,
        fetched_at=_NOW,
        source_kind="source_drive_file",
        source_version="rev-12345",
        selection_method=SelectionMethod.STABLE_ID,
    )
    base.update(overrides)
    return base


def test_output_constructs_with_canonical_kwargs():
    o = SourceOutputContract(**_output_kwargs())
    assert o.content_size == 1234


def test_output_content_hash_accepts_valid_sha256():
    SourceOutputContract(**_output_kwargs(content_hash=_VALID_HASH))


@pytest.mark.parametrize(
    "bad_hash",
    [
        "a" * 64,                         # no prefix
        "md5:" + "a" * 32,                # wrong algo
        "sha256:" + "a" * 32,             # short digest
        "sha256:" + "A" * 64,             # uppercase
        "sha256:" + "z" * 64,             # non-hex
    ],
)
def test_output_content_hash_rejects_invalid(bad_hash):
    with pytest.raises(ValidationError):
        SourceOutputContract(**_output_kwargs(content_hash=bad_hash))


def test_output_content_path_rejects_absolute():
    with pytest.raises(ValidationError, match="relative"):
        SourceOutputContract(
            **_output_kwargs(content_path="/etc/passwd")
        )


def test_output_content_path_rejects_parent_traversal():
    with pytest.raises(ValidationError, match="\\.\\."):
        SourceOutputContract(
            **_output_kwargs(content_path="data/../../etc/passwd")
        )


def test_output_content_size_non_negative():
    with pytest.raises(ValidationError):
        SourceOutputContract(**_output_kwargs(content_size=-1))


def test_output_content_size_zero_allowed():
    SourceOutputContract(**_output_kwargs(content_size=0))


def test_output_source_version_optional():
    kwargs = _output_kwargs()
    kwargs.pop("source_version")
    SourceOutputContract(**kwargs)


def test_output_drift_guard_every_selection_method_accepted():
    for method in SelectionMethod:
        SourceOutputContract(
            **_output_kwargs(selection_method=method)
        )


def test_output_extra_field_rejected():
    with pytest.raises(ValidationError):
        SourceOutputContract(**_output_kwargs(checksum="abc"))
