"""V2 scheduler — registry cache package.

Phase 6 ships the registry cache layer for Slack channels and
Google Sheets / Docs items per design §5.7. Pure storage +
lookup layer; no ADK tool surface, no ``boot_runtime``
integration, no real Slack / Drive client construction. Phase
7 (typed authoring tools) wires the cache into the authoring
path and adds the on-demand refresh ADK tool.

Public surface (slice 5 finalisation):

- **Errors:** :class:`RegistryCacheError` + five subclasses
  (:class:`NoCacheAvailable`, :class:`CacheMiss`,
  :class:`WorkspaceMismatch`, :class:`NoCacheAndNetworkDown`,
  :class:`ChannelAmbiguous`).
- **Schemas:** three cache models
  (:class:`SlackChannelsCache`, :class:`GoogleSheetsCache`,
  :class:`GoogleDocsCache`) + three entry classes +
  :data:`CacheKind` / :data:`CacheFile` aliases.
- **Paths:** :data:`DEFAULT_CACHE_BASE` + :func:`cache_path`.
- **Load / save / freshness:** :func:`load_cache`,
  :func:`save_cache`, :func:`is_stale`.
- **Refresh primitives:** :class:`SlackChannelsClient`,
  :class:`GoogleDriveClient` Protocols +
  :func:`refresh_slack_channels`,
  :func:`refresh_google_sheets`, :func:`refresh_google_docs`.
- **Lookup helpers:** :func:`resolve_channel`,
  :func:`resolve_sheet`, :func:`resolve_doc`.

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
from app.v2.registry_cache.loader import (
    is_stale,
    load_cache,
    save_cache,
)
from app.v2.registry_cache.paths import DEFAULT_CACHE_BASE, cache_path
from app.v2.registry_cache.refresh import (
    GoogleDriveClient,
    SlackChannelsClient,
    refresh_google_docs,
    refresh_google_sheets,
    refresh_slack_channels,
)
from app.v2.registry_cache.resolver import (
    resolve_channel,
    resolve_doc,
    resolve_sheet,
)
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
    "GoogleDriveClient",
    "GoogleSheetsCache",
    "GoogleSheetsEntry",
    "NoCacheAndNetworkDown",
    "NoCacheAvailable",
    "RegistryCacheError",
    "SlackChannelEntry",
    "SlackChannelsCache",
    "SlackChannelsClient",
    "WorkspaceMismatch",
    "cache_path",
    "is_stale",
    "load_cache",
    "refresh_google_docs",
    "refresh_google_sheets",
    "refresh_slack_channels",
    "resolve_channel",
    "resolve_doc",
    "resolve_sheet",
    "save_cache",
]
