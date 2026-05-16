"""v2 scheduler — source LOADER registry (instances).

Phase 10 slice 2 per ``docs/PHASE_10_PLAN.md`` §1.1 / §3.3.

``app.v2.registry.SOURCES`` is the canonical *descriptor*
registry (``SourceDescriptor`` keyed by id — the read-only
invariant + tag policy live there, design §5.3.6 / §5.4).
It holds NO loader callable. The slice-8 resolver needs to
dispatch the actual loader by id, so this module adds the
parallel *instance* registry: :class:`SourceLoaderRegistry`
keyed by ``loader.descriptor.id``.

:func:`register_source_loader` is the single registration
seam — it registers the descriptor into the descriptor
registry (which re-asserts the read-only invariant) AND
the loader instance into the loader registry, refusing the
pair if EITHER side already knows the id (no half-
registered state).

Production populates the module singletons once at import
(``app/v2/sources/__init__.py``); tests construct fresh
registries and pass them explicitly to keep the pool
isolated (per the ``app.v2.registry`` singleton note).
"""

from __future__ import annotations

from typing import Iterator, Optional

from app.v2.registry import (
    SOURCES,
    DuplicateDescriptorError,
    SourceRegistry,
    UnknownSourceError,
)
from app.v2.sources.contract import SourceLoader


class SourceLoaderRegistry:
    """Registry of :class:`SourceLoader` instances keyed by
    ``loader.descriptor.id``. Mirrors the
    ``app.v2.registry._BaseRegistry`` surface (dup-reject,
    ``require`` / ``lookup``) without forcing a Protocol
    through the descriptor-typed generic base."""

    def __init__(self) -> None:
        self._store: dict[str, SourceLoader] = {}

    def register(self, loader: SourceLoader) -> None:
        if not isinstance(loader, SourceLoader):
            raise TypeError(
                "SourceLoaderRegistry.register expects a "
                "SourceLoader (with .descriptor + async "
                f"load); got {type(loader).__name__}"
            )
        key = loader.descriptor.id
        if key in self._store:
            raise DuplicateDescriptorError(
                f"SourceLoaderRegistry: duplicate loader for "
                f"key {key!r}"
            )
        self._store[key] = loader

    def lookup(self, key: str) -> Optional[SourceLoader]:
        return self._store.get(key)

    def require(self, key: str) -> SourceLoader:
        loader = self._store.get(key)
        if loader is None:
            raise UnknownSourceError(key)
        return loader

    def __contains__(self, key: object) -> bool:
        return key in self._store

    def __iter__(self) -> Iterator[SourceLoader]:
        return iter(self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def keys(self) -> list[str]:
        return list(self._store.keys())

    def clear(self) -> None:
        """Empty the registry. Test isolation only — never
        from production code."""
        self._store.clear()

    def _rollback(self, key: str) -> None:
        """Undo a just-applied :meth:`register` for ``key``.

        Internal — used ONLY by
        :func:`register_source_loader` to keep paired
        registration atomic when the descriptor-side step
        fails after the loader-side step succeeded. Idempotent
        (pop-if-present)."""
        self._store.pop(key, None)


#: Production loader-instance singleton. Populated once at
#: import by ``app/v2/sources/__init__.py``.
SOURCE_LOADERS: SourceLoaderRegistry = SourceLoaderRegistry()


def register_source_loader(
    loader: SourceLoader,
    *,
    sources: SourceRegistry = SOURCES,
    loaders: SourceLoaderRegistry = SOURCE_LOADERS,
) -> None:
    """Register ``loader`` into BOTH the descriptor registry
    (``sources``) and the loader-instance registry
    (``loaders``) **atomically**.

    Refuses the pair if EITHER registry already knows the
    id. Registration is all-or-nothing: the loader is
    inserted first, then the descriptor (which re-asserts
    the source read-only tag invariant and may raise). If
    the descriptor step fails, the loader insert is rolled
    back so NEITHER side is left registered — there is no
    split-brain (codex slice-2 🟡: a second-step failure
    after the first step previously left the descriptor
    registered with no loader, permanently).
    """
    key = loader.descriptor.id
    if key in loaders or sources.lookup(key) is not None:
        raise DuplicateDescriptorError(
            f"source loader {key!r} already registered "
            "(descriptor and/or loader registry)"
        )
    # Step 1: the loader registry — the one we own and can
    # roll back. Step 2: the descriptor registry, which
    # validates the read-only invariant and may raise. A
    # step-2 failure undoes step 1 → all-or-nothing.
    loaders.register(loader)
    try:
        sources.register(loader.descriptor)
    except BaseException:
        loaders._rollback(key)
        raise


__all__ = [
    "SourceLoaderRegistry",
    "SOURCE_LOADERS",
    "register_source_loader",
]
