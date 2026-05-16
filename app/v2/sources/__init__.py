"""v2 scheduler — source loader layer (phase 10, §12 step 10).

The four phase-1 concrete source loaders + the per-fire
snapshot writer + the per-source cache / fallback /
live-change layer. **Build-the-layer phase**: registered
into the ``SOURCES`` registry and unit-pinned, but NOT
wired into the worker fire path — source-driven schedules
begin firing at step 11 (source templates). See
``docs/PHASE_10_PLAN.md``.

Shared surface (slice 1):
- :mod:`app.v2.sources.errors` — the typed ``SourceError``
  hierarchy (exactly one ``fallback_eligible`` subclass).
- :mod:`app.v2.sources.contract` — :class:`SourceResult`,
  the :class:`SourceLoader` protocol, and the §3.6
  canonical-bytes helper.

Loader registry + ``source_literal`` (slice 2):
- :mod:`app.v2.sources.registry` —
  :class:`SourceLoaderRegistry` + the
  :func:`register_source_loader` seam.
- :mod:`app.v2.sources.literal` — the ``source_literal``
  loader.

Importing this package REGISTERS the built-in loaders into
the production ``SOURCES`` / ``SOURCE_LOADERS`` singletons
ONCE (idempotent — a re-entrant import is a no-op). Tests
that need isolation build fresh registries and call the
per-loader ``register_*`` helpers explicitly.

Snapshot writer + cache + resolver land in slices 4-8.
"""

from __future__ import annotations

from app.v2.registry import SOURCES
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
from app.v2.sources.literal import (
    SOURCE_LITERAL_ID,
    LiteralSource,
    literal_source,
    register_literal,
)
from app.v2.sources.registry import (
    SOURCE_LOADERS,
    SourceLoaderRegistry,
    register_source_loader,
)


def _register_builtin_sources() -> None:
    """Register every built-in loader into the production
    singletons. Idempotent: skips a loader whose id is
    already present so a re-entrant / repeated import never
    raises ``DuplicateDescriptorError``."""
    if SOURCE_LITERAL_ID not in SOURCE_LOADERS and (
        SOURCES.lookup(SOURCE_LITERAL_ID) is None
    ):
        register_literal()


_register_builtin_sources()


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
    "SourceLoaderRegistry",
    "SOURCE_LOADERS",
    "register_source_loader",
    "SOURCE_LITERAL_ID",
    "LiteralSource",
    "literal_source",
    "register_literal",
]
