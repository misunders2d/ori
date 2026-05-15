"""V2 scheduler — registry cache on-disk path resolver.

Canonical filename per kind under :data:`DEFAULT_CACHE_BASE`.
Tests override via the ``base`` keyword so writes land inside
``tmp_path``.

Phase 6 slice 1 per ``docs/PHASE_6_PLAN.md`` §3.3.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7 (cache layout)
- ``docs/PHASE_6_PLAN.md`` §3.3
"""

from __future__ import annotations

import pathlib
from typing import Optional

from app.v2.registry_cache.schemas import CacheKind


DEFAULT_CACHE_BASE: pathlib.Path = pathlib.Path("data/cache/registry")
"""Default on-disk root for registry cache files.

Phase 6 nests under ``data/cache/registry/`` so future cache
subsystems (source snapshots in phase 10 etc.) get sibling
directories without colliding.
"""


_FILENAMES: dict[CacheKind, str] = {
    "slack_channels": "slack_channels.json",
    "google_sheets_items": "google_sheets_items.json",
    "google_docs_items": "google_docs_items.json",
}


def cache_path(
    kind: CacheKind, *, base: Optional[pathlib.Path] = None
) -> pathlib.Path:
    """Resolve the on-disk path for the cache file of ``kind``.

    ``base`` defaults to :data:`DEFAULT_CACHE_BASE`; tests
    override via ``base=tmp_path``. Unknown ``kind`` raises
    :class:`KeyError` so a typo in caller code surfaces
    instead of silently writing to the wrong place.
    """
    root = base if base is not None else DEFAULT_CACHE_BASE
    return root / _FILENAMES[kind]


__all__ = ["DEFAULT_CACHE_BASE", "cache_path"]
