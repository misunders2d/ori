"""v2 scheduler — SourceRefSpec (per-source policy bundle).

Phase 10 slice 1 per ``docs/PHASE_10_PLAN.md`` §1.8 / §9
Q1 (codex plan-review round-1: RATIFIED — reuse the
existing policy models, no duplicate).

A :class:`SourceRefSpec` is the typed home for a single
LiveSourceRef's per-source policy. It **composes** the
already-shipped policy models rather than duplicating
them:

- ``cache`` — the existing
  :class:`app.v2.models.common.LiveSourceCachePolicy`
  (``cache_ttl_seconds`` / ``stale_max_age_seconds`` /
  ``fallback_policy``), reused verbatim.
- ``live_change_policy`` — the genuinely-missing field;
  :class:`app.v2.enums.LiveChangePolicy` already exists
  but no model carried it.
- ``explicit_default`` — required iff
  ``cache.fallback_policy ==
  SourceFallbackPolicy.ALERT_AND_USE_DEFAULT`` (design
  §5.3.2: that policy is valid only when a user-explicit
  default is present AND shown in dry-run); forbidden
  otherwise so a stray default can't mask a misconfigured
  fallback.

Retention is NOT re-declared here — it lives on
``ScheduleSpec.audit`` (:class:`AuditPolicy`,
``app/v2/models/common.py``). ``SourceRefSpec`` is purely
the cache / live-change / default bundle.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.types import JsonValue

from app.v2.enums import LiveChangePolicy, SourceFallbackPolicy
from app.v2.models.common import LiveSourceCachePolicy


_LOADER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class SourceRefSpec(BaseModel):
    """The frozen per-source policy reference for a
    LiveSourceRef input.

    ``loader`` names a key in the ``SOURCES`` registry
    (slice-2+ loaders register there); phase-10 keeps the
    field a snake_case string — registry-existence
    validation is the resolver's job at fire time (slice
    8), not a model concern.
    """

    model_config = ConfigDict(extra="forbid")

    loader: str = Field(
        description="Registered SOURCES key (snake_case). "
        "Existence is checked by the resolver at fire time, "
        "not by this model."
    )
    args: dict[str, JsonValue] = Field(default_factory=dict)
    cache: LiveSourceCachePolicy
    live_change_policy: LiveChangePolicy
    explicit_default: Optional[JsonValue] = Field(
        default=None,
        description="The contract's explicitly-declared "
        "default value. REQUIRED iff "
        "cache.fallback_policy == alert_and_use_default; "
        "FORBIDDEN otherwise.",
    )

    @field_validator("loader")
    @classmethod
    def _loader_snake_case(cls, v: str) -> str:
        if not _LOADER_PATTERN.fullmatch(v):
            raise ValueError(
                "SourceRefSpec.loader must match "
                "^[a-z][a-z0-9_]*$"
            )
        return v

    @model_validator(mode="after")
    def _explicit_default_pairing(self) -> "SourceRefSpec":
        needs_default = (
            self.cache.fallback_policy
            == SourceFallbackPolicy.ALERT_AND_USE_DEFAULT
        )
        has_default = self.explicit_default is not None
        if needs_default and not has_default:
            raise ValueError(
                "explicit_default is REQUIRED when "
                "cache.fallback_policy == "
                "'alert_and_use_default' (design §5.3.2: "
                "that policy is valid only with a "
                "user-explicit default shown in dry-run)."
            )
        if has_default and not needs_default:
            raise ValueError(
                "explicit_default is FORBIDDEN unless "
                "cache.fallback_policy == "
                "'alert_and_use_default' — a stray default "
                "must not mask a misconfigured fallback "
                "policy."
            )
        return self


__all__ = ["SourceRefSpec"]
