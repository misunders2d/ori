"""Phase 10 slice 1 — typed source-error taxonomy.

Per ``docs/PHASE_10_PLAN.md`` §3.5 / §5. The fallback
surface must be unforgeable: EXACTLY ONE subclass
(``SourceFetchError``) is ``fallback_eligible``; every
other ``SourceError`` subclass is non-fallback by
construction (base default ``False``). A future subclass
that forgets to opt in is non-fallback automatically.
"""

from __future__ import annotations

import pytest

from app.v2.sources.errors import (
    SourceAuthError,
    SourceError,
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
    SourceSecurityError,
)


_NON_FETCH = [
    SourceAuthError,
    SourceSecurityError,
    SourcePolicyError,
    SourceParseError,
]
_ALL_SUBCLASSES = [SourceFetchError, *_NON_FETCH]


def test_all_subclass_source_error():
    for cls in _ALL_SUBCLASSES:
        assert issubclass(cls, SourceError)
        assert issubclass(cls, Exception)


def test_base_default_is_non_fallback():
    """Fail-safe: the base default must be False so a new
    subclass is non-fallback unless it explicitly opts in."""
    assert SourceError.fallback_eligible is False


def test_only_fetch_error_is_fallback_eligible():
    assert SourceFetchError.fallback_eligible is True
    for cls in _NON_FETCH:
        assert cls.fallback_eligible is False, (
            f"{cls.__name__} must be non-fallback — only "
            f"SourceFetchError may serve a cached snapshot"
        )


def test_fallback_eligible_holds_on_instances():
    """The resolver branches on the raised instance; the
    ClassVar must read through the instance too."""
    assert SourceFetchError("x").fallback_eligible is True
    for cls in _NON_FETCH:
        assert cls("x").fallback_eligible is False


def test_no_non_fetch_isinstance_leak():
    """A non-fetch error must NOT satisfy
    isinstance(exc, SourceFetchError) — otherwise it would
    leak into the fallback branch."""
    for cls in _NON_FETCH:
        assert not isinstance(cls("x"), SourceFetchError)


def test_each_carries_a_distinct_machine_code():
    codes = {cls: cls.code for cls in _ALL_SUBCLASSES}
    assert len(set(codes.values())) == len(_ALL_SUBCLASSES), (
        f"taxonomy codes must be distinct: {codes}"
    )
    assert all(isinstance(c, str) and c for c in codes.values())


def test_payload_code_defaults_to_class_code():
    e = SourceAuthError("token expired")
    assert e.payload_code == SourceAuthError.code
    assert e.message == "token expired"


def test_instance_code_refines_payload_not_taxonomy():
    """An instance may carry a more specific payload code
    (e.g. the fence reason) WITHOUT changing the class
    taxonomy / fallback rule."""
    e = SourceSecurityError(
        "denied", code="path_outside_allowed_root"
    )
    assert e.payload_code == "path_outside_allowed_root"
    # Taxonomy + fallback rule unchanged.
    assert e.code == "source_security_error"
    assert e.fallback_eligible is False
    assert isinstance(e, SourceError)


def test_base_not_raised_directly_is_still_constructible():
    """SourceError is the base; constructing it is allowed
    (helpers may catch it) but it is non-fallback."""
    e = SourceError("generic")
    assert e.fallback_eligible is False
    with pytest.raises(SourceError):
        raise e
