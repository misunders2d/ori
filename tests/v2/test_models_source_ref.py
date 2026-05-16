"""Phase 10 slice 1 — SourceRefSpec.

Per ``docs/PHASE_10_PLAN.md`` §1.8 / §9 Q1 / §5.
``SourceRefSpec`` COMPOSES the existing
``LiveSourceCachePolicy`` (cache) + ``LiveChangePolicy``
(enum); it does NOT re-declare retention (that lives on
``ScheduleSpec.audit`` / ``AuditPolicy``). The
``explicit_default`` validator: required iff
``cache.fallback_policy == alert_and_use_default``,
forbidden otherwise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.v2.enums import LiveChangePolicy, SourceFallbackPolicy
from app.v2.models.common import AuditPolicy, LiveSourceCachePolicy
from app.v2.models.source_ref import SourceRefSpec


def _cache(policy: SourceFallbackPolicy) -> LiveSourceCachePolicy:
    return LiveSourceCachePolicy(
        cache_ttl_seconds=300,
        stale_max_age_seconds=3600,
        fallback_policy=policy,
    )


def test_composes_existing_policy_models():
    s = SourceRefSpec(
        loader="source_drive_file",
        args={"file_id": "x"},
        cache=_cache(SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT),
        live_change_policy=LiveChangePolicy.ALLOW,
    )
    assert isinstance(s.cache, LiveSourceCachePolicy)
    assert s.cache.cache_ttl_seconds == 300
    assert s.live_change_policy is LiveChangePolicy.ALLOW
    assert s.explicit_default is None


def test_no_retention_fields_on_source_ref():
    """Retention belongs to AuditPolicy on
    ScheduleSpec.audit — SourceRefSpec must NOT carry a
    duplicate (codex round-1 🟡 / Q1)."""
    fields = set(SourceRefSpec.model_fields)
    audit_fields = set(AuditPolicy.model_fields)
    assert fields.isdisjoint(audit_fields), (
        f"SourceRefSpec must not duplicate AuditPolicy "
        f"fields; overlap={fields & audit_fields}"
    )
    assert "keep_last_n_snapshots" not in fields
    assert "on_oversize" not in fields


def test_explicit_default_required_for_alert_and_use_default():
    with pytest.raises(ValidationError, match="REQUIRED"):
        SourceRefSpec(
            loader="source_literal",
            cache=_cache(SourceFallbackPolicy.ALERT_AND_USE_DEFAULT),
            live_change_policy=LiveChangePolicy.ALLOW,
        )
    # present → ok
    s = SourceRefSpec(
        loader="source_literal",
        cache=_cache(SourceFallbackPolicy.ALERT_AND_USE_DEFAULT),
        live_change_policy=LiveChangePolicy.ALLOW,
        explicit_default={"text": "fallback copy"},
    )
    assert s.explicit_default == {"text": "fallback copy"}


def test_explicit_default_forbidden_for_other_policies():
    for pol in (
        SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
        SourceFallbackPolicy.ALERT_AND_SKIP,
    ):
        with pytest.raises(ValidationError, match="FORBIDDEN"):
            SourceRefSpec(
                loader="source_literal",
                cache=_cache(pol),
                live_change_policy=LiveChangePolicy.ALLOW,
                explicit_default="stray",
            )


def test_loader_must_be_snake_case():
    with pytest.raises(ValidationError):
        SourceRefSpec(
            loader="Source-Drive",
            cache=_cache(SourceFallbackPolicy.ALERT_AND_SKIP),
            live_change_policy=LiveChangePolicy.ALLOW,
        )


def test_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SourceRefSpec(
            loader="source_literal",
            cache=_cache(SourceFallbackPolicy.ALERT_AND_SKIP),
            live_change_policy=LiveChangePolicy.ALLOW,
            keep_last_n_snapshots=5,  # retention belongs on AuditPolicy
        )


def test_live_change_policy_required():
    with pytest.raises(ValidationError):
        SourceRefSpec(
            loader="source_literal",
            cache=_cache(SourceFallbackPolicy.ALERT_AND_SKIP),
        )
