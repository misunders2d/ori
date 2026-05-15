"""V2 scheduler — registry cache load + save + staleness.

Phase 6 slice 2 per ``docs/PHASE_6_PLAN.md`` §3.4 + §5.4.

Three functions:

- :func:`load_cache` — read + parse the on-disk cache for a
  given kind. Returns ``None`` for an absent file OR an
  owner-id mismatch; raises :class:`RegistryCacheError` on
  corrupt JSON / schema-invalid payload / wrong kind
  discriminator.
- :func:`save_cache` — atomic tmp+rename write. Raises
  :class:`ValueError` BEFORE any I/O when ``snapshot.kind !=
  kind`` (reviewer round-1 L314 fix).
- :func:`is_stale` — pure freshness predicate. Returns
  ``now - cache.fetched_at > ttl``. Imports no clock; caller
  passes its own ``now``.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7
- ``docs/PHASE_6_PLAN.md`` §3.4 / §5.4
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import datetime, timedelta
from typing import Optional

from pydantic import ValidationError

from app.v2.registry_cache.errors import RegistryCacheError
from app.v2.registry_cache.paths import cache_path
from app.v2.registry_cache.schemas import (
    CacheFile,
    CacheKind,
    GoogleDocsCache,
    GoogleSheetsCache,
    SlackChannelsCache,
)


_logger = logging.getLogger(__name__)


_CACHE_CLASSES: dict[CacheKind, type[CacheFile]] = {
    "slack_channels": SlackChannelsCache,
    "google_sheets_items": GoogleSheetsCache,
    "google_docs_items": GoogleDocsCache,
}


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load_cache(
    kind: CacheKind,
    *,
    expected_owner_id: Optional[str] = None,
    base: Optional[pathlib.Path] = None,
) -> Optional[CacheFile]:
    """Read + parse the cache for ``kind``.

    Returns ``None`` when:

    - the file is absent on disk (no exception — the caller
      decides whether to refresh or raise via
      :class:`NoCacheAvailable`);
    - ``expected_owner_id`` is provided AND the cached
      ``owner_id`` property does NOT match. The mismatch is
      logged at WARNING; the on-disk file is left untouched
      so a manual review is possible (per Q7 — no rename).

    Raises :class:`RegistryCacheError` on:

    - corrupt JSON;
    - schema-invalid payload (any pydantic ``ValidationError``,
      including a wrong ``kind`` discriminator or a non-UTC
      ``fetched_at``);
    - a payload whose declared ``kind`` does not match the
      requested ``kind`` (defence in depth — the schema's
      ``Literal`` discriminator should catch this first, but
      a hand-edited file that swapped the literal could
      otherwise slip through).
    """
    path = cache_path(kind, base=base)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    cls = _CACHE_CLASSES[kind]
    try:
        cache = cls.model_validate_json(raw)
    except (ValidationError, json.JSONDecodeError) as exc:
        raise RegistryCacheError(
            f"registry cache at {path} failed to parse for "
            f"kind={kind!r}: {exc}"
        ) from exc

    if cache.kind != kind:
        raise RegistryCacheError(
            f"registry cache at {path} declares kind={cache.kind!r}, "
            f"expected {kind!r}"
        )

    if expected_owner_id is not None and cache.owner_id != expected_owner_id:
        _logger.warning(
            "registry cache owner mismatch: "
            "kind=%s expected=%s found=%s path=%s",
            kind,
            expected_owner_id,
            cache.owner_id,
            path,
        )
        return None

    return cache


# ---------------------------------------------------------------------------
# Save (atomic tmp+rename)
# ---------------------------------------------------------------------------


def save_cache(
    kind: CacheKind,
    snapshot: CacheFile,
    *,
    base: Optional[pathlib.Path] = None,
) -> None:
    """Write ``snapshot`` to the on-disk path for ``kind``
    atomically via tmp + rename.

    Reviewer round-1 L314 fix: if ``snapshot.kind != kind``
    raises :class:`ValueError` BEFORE any I/O — phase 6 must
    never persist a docs payload at the slack path (or any
    other kind-channel pair). The check is the first line of
    the function so callers see a hard fail rather than a
    silent corrupting write.

    Atomic semantics:
    - The tmp file lives in the same parent directory as the
      target so :func:`os.rename` is atomic on the local
      filesystem (POSIX guarantee).
    - The tmp name carries the current process pid so two
      concurrent writers do not clobber each other's tmp
      files.
    - On a successful call the tmp file is renamed to the
      target and no ``.tmp.*`` artifact remains in the
      cache directory.
    """
    if snapshot.kind != kind:
        raise ValueError(
            f"save_cache kind mismatch: requested kind={kind!r} "
            f"but snapshot.kind={snapshot.kind!r}"
        )

    path = cache_path(kind, base=base)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = path.parent / f"{path.name}.tmp.{os.getpid()}"
    tmp_path.write_text(
        snapshot.model_dump_json(indent=2), encoding="utf-8"
    )
    os.rename(tmp_path, path)


# ---------------------------------------------------------------------------
# Freshness predicate
# ---------------------------------------------------------------------------


def is_stale(
    cache: CacheFile,
    *,
    now: datetime,
    ttl: timedelta = timedelta(hours=24),
) -> bool:
    """Pure freshness predicate (per Q5 answer).

    Returns ``True`` when ``now - cache.fetched_at > ttl``.

    Phase 6 ships the helper but does NOT log on staleness —
    the caller (phase 7 authoring) decides whether to warn,
    refresh, or proceed. ``now`` is the caller's clock output;
    the helper itself does not import a clock to keep
    ``_defaults.py`` the sole ``datetime.now`` site
    (phase-5 hard rule 10).
    """
    return now - cache.fetched_at > ttl


__all__ = ["is_stale", "load_cache", "save_cache"]
