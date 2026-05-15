"""V2 scheduler — registry cache package.

Phase 6 ships the registry cache layer for Slack channels and
Google Sheets / Docs items per design §5.7. Phase 7 (typed
authoring tools) will consume the resolver.

Slice 1 surface: errors + schemas + paths. Later slices add
``loader``, ``refresh``, ``resolver``; the export list grows
with each slice. ``__init__.py`` is finalised in slice 5.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7
- ``docs/PHASE_6_PLAN.md``
"""

from app.v2.registry_cache.errors import (
    CacheMiss,
    ChannelAmbiguous,
    NoCacheAndNetworkDown,
    NoCacheAvailable,
    RegistryCacheError,
    WorkspaceMismatch,
)
from app.v2.registry_cache.paths import DEFAULT_CACHE_BASE, cache_path
from app.v2.registry_cache.schemas import (
    CacheFile,
    CacheKind,
    GoogleDocsCache,
    GoogleDocsEntry,
    GoogleSheetsCache,
    GoogleSheetsEntry,
    SlackChannelEntry,
    SlackChannelsCache,
)


__all__ = [
    "CacheFile",
    "CacheKind",
    "CacheMiss",
    "ChannelAmbiguous",
    "DEFAULT_CACHE_BASE",
    "GoogleDocsCache",
    "GoogleDocsEntry",
    "GoogleSheetsCache",
    "GoogleSheetsEntry",
    "NoCacheAndNetworkDown",
    "NoCacheAvailable",
    "RegistryCacheError",
    "SlackChannelEntry",
    "SlackChannelsCache",
    "WorkspaceMismatch",
    "cache_path",
]
