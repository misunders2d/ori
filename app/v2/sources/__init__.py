"""v2 scheduler — source loader layer (phase 10, §12 step 10).

The four phase-1 concrete source loaders + the per-fire
snapshot writer + the per-source cache / fallback /
live-change layer. **Build-the-layer phase**: registered
into the ``SOURCES`` registry and unit-pinned, but NOT
wired into the worker fire path — source-driven schedules
begin firing at step 11 (source templates). See
``docs/PHASE_10_PLAN.md``.

Slice 1 ships the shared surface only:
- :mod:`app.v2.sources.errors` — the typed ``SourceError``
  hierarchy (exactly one ``fallback_eligible`` subclass).
- :mod:`app.v2.sources.contract` — :class:`SourceResult`,
  the :class:`SourceLoader` protocol, and the §3.6
  canonical-bytes helper.

Concrete loaders + snapshot writer + cache + resolver land
in slices 2-8.
"""

from __future__ import annotations

from app.v2.sources.contract import (
    SourceLoader,
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import (
    SourceAuthError,
    SourceError,
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
    SourceSecurityError,
)

__all__ = [
    "SourceError",
    "SourceFetchError",
    "SourceAuthError",
    "SourceSecurityError",
    "SourcePolicyError",
    "SourceParseError",
    "SourceResult",
    "SourceLoader",
    "canonical_bytes",
    "content_hash_for",
]
