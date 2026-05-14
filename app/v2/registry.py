"""V2 registry layer — descriptor storage, no execution.

Three typed registries hold the descriptors a v2 schedule may
reference at authoring time:

- :class:`ToolRegistry`   keyed by ``ToolDescriptor.name``
- :class:`SourceRegistry` keyed by ``SourceDescriptor.id``
- :class:`EmitRegistry`   keyed by ``EmitDescriptor.id``

A descriptor is just metadata. The registry exposes:

- ``register(descriptor)`` — add to the registry. Refuses
  duplicate keys (``DuplicateDescriptorError``) and refuses
  objects that are not instances of the registry's descriptor
  type.
- ``lookup(key)`` — descriptor or ``None``.
- ``require(key)`` — descriptor or raises
  ``UnknownToolError`` / ``UnknownSourceError`` / ``UnknownEmitError``.
- ``ToolRegistry.tags_for(name)`` — fail-safe tag set: the
  descriptor's tags if registered, else
  :data:`FAIL_SAFE_UNKNOWN_TAGS` (per design §5.4 final
  paragraph).
- ``__contains__`` / iteration / ``clear`` (for tests).

The registry is metadata only — there is no ``dispatch``,
``invoke``, or ``call`` method. Adapter execution lives in a
later phase and routes through a separate runtime layer.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4 (fail-safe default
  tag set), §5.9 ("previously-unused adapter" friction)
- ``docs/PHASE_2_PLAN.md`` §3 + §4
"""

from __future__ import annotations

from typing import Generic, Iterator, Optional, TypeVar

from app.v2.descriptors.emit import EmitDescriptor
from app.v2.descriptors.source import SourceDescriptor
from app.v2.descriptors.tool import ToolDescriptor
from app.v2.tool_tags import ToolCapabilityTag


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DuplicateDescriptorError(ValueError):
    """Raised when ``register`` would overwrite an existing key.

    The registry is append-only at boot — a duplicate id / name
    is almost always a copy-paste mistake in the registration
    module, not an intentional override. Loud failure beats a
    silent shadow.
    """


class UnknownToolError(KeyError):
    """Raised by ``ToolRegistry.require`` when the name is not
    registered."""


class UnknownSourceError(KeyError):
    """Raised by ``SourceRegistry.require`` when the id is not
    registered."""


class UnknownEmitError(KeyError):
    """Raised by ``EmitRegistry.require`` when the id is not
    registered."""


class InvalidSourceDescriptorError(ValueError):
    """Raised by ``SourceRegistry.register`` when a constructed
    descriptor violates the read-only invariant.

    ``SourceDescriptor`` Pydantic validators already enforce
    this at construction. The registry re-checks as a belt at
    the boundary so a caller using ``model_construct`` (which
    skips validators) cannot smuggle a write-tagged source past.
    """


class InvalidEmitDescriptorError(ValueError):
    """Raised by ``EmitRegistry.register`` when the descriptor
    is not write-side at all.

    Same belt-and-suspenders rationale as
    ``InvalidSourceDescriptorError``.
    """


# ---------------------------------------------------------------------------
# Fail-safe tag set for unknown tools (design §5.4 final line)
# ---------------------------------------------------------------------------


FAIL_SAFE_UNKNOWN_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {ToolCapabilityTag.WRITE_EXTERNAL}
)
"""Returned by :meth:`ToolRegistry.tags_for` when the requested
name is unknown.

This is the strict reading of design §5.4: "Default tag set for
new tools: write_external (fail-safe)". The intent is that a
read-only reasoning step trying to call an unrecognised tool
gets blocked (``is_blocked_by_read_only_reasoning`` returns
True). It is NOT the friction-gate / admin-approval signal —
that comes from a separate runtime check tied to "previously-
unused adapter" history (design §5.9), not from the registry.
"""


# ---------------------------------------------------------------------------
# Base registry
# ---------------------------------------------------------------------------


D = TypeVar(
    "D",
    ToolDescriptor,
    SourceDescriptor,
    EmitDescriptor,
)


class _BaseRegistry(Generic[D]):
    """Typed key→descriptor map with duplicate detection.

    Subclasses pin the descriptor type, supply ``_key_of`` to
    extract the registry key, and define the ``unknown_error``
    subclass to raise from ``require``.
    """

    descriptor_type: type
    unknown_error: type

    def __init__(self) -> None:
        self._store: dict[str, D] = {}

    # Subclass-specific extraction. Override in each registry.
    def _key_of(self, descriptor: D) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _validate(self, descriptor: D) -> None:
        """Hook for descriptor-type-specific belt checks.

        Subclasses override to enforce invariants the descriptor
        ought to have satisfied at construction (Pydantic
        validators) but which the registry re-asserts as a
        safety net for callers using ``model_construct``.

        Default: no-op.
        """

    def register(self, descriptor: D) -> None:
        if not isinstance(descriptor, self.descriptor_type):
            raise TypeError(
                f"{type(self).__name__}.register expects a "
                f"{self.descriptor_type.__name__}; got "
                f"{type(descriptor).__name__}"
            )
        self._validate(descriptor)
        key = self._key_of(descriptor)
        if key in self._store:
            raise DuplicateDescriptorError(
                f"{type(self).__name__}: duplicate registration for "
                f"key {key!r}"
            )
        self._store[key] = descriptor

    def lookup(self, key: str) -> Optional[D]:
        return self._store.get(key)

    def require(self, key: str) -> D:
        d = self._store.get(key)
        if d is None:
            raise self.unknown_error(key)
        return d

    def __contains__(self, key: object) -> bool:
        return key in self._store

    def __iter__(self) -> Iterator[D]:
        return iter(self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def keys(self) -> list[str]:
        """Snapshot of registered keys in insertion order."""
        return list(self._store.keys())

    def clear(self) -> None:
        """Empty the registry. Intended for test isolation — do
        NOT call from production code."""
        self._store.clear()


# ---------------------------------------------------------------------------
# Concrete registries
# ---------------------------------------------------------------------------


class ToolRegistry(_BaseRegistry[ToolDescriptor]):
    """Registry of ``ToolDescriptor`` keyed by ``name``."""

    descriptor_type = ToolDescriptor
    unknown_error = UnknownToolError

    def _key_of(self, descriptor: ToolDescriptor) -> str:
        return descriptor.name

    def tags_for(self, name: str) -> frozenset[ToolCapabilityTag]:
        """Return the descriptor's tag set if registered, else
        the fail-safe set (per design §5.4).

        Callers that need to detect "previously-unused adapter"
        should use ``lookup`` / ``__contains__`` directly —
        ``tags_for`` is the policy-level lookup, not the
        existence query.
        """
        d = self.lookup(name)
        if d is None:
            return FAIL_SAFE_UNKNOWN_TAGS
        return frozenset(d.tags)


# Forbidden tags on a source descriptor — duplicates the set in
# app/v2/descriptors/source.py for the belt-check, since that
# module's set is private.
_SOURCE_FORBIDDEN_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {
        ToolCapabilityTag.WRITE_EXTERNAL,
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    }
)


class SourceRegistry(_BaseRegistry[SourceDescriptor]):
    """Registry of ``SourceDescriptor`` keyed by ``id``."""

    descriptor_type = SourceDescriptor
    unknown_error = UnknownSourceError

    def _key_of(self, descriptor: SourceDescriptor) -> str:
        return descriptor.id

    def _validate(self, descriptor: SourceDescriptor) -> None:
        # Belt: even if a caller built the descriptor with
        # model_construct() (which bypasses Pydantic validators),
        # the registry refuses anything that violates the
        # read-only invariant.
        tags = set(getattr(descriptor, "tags", set()) or set())
        if ToolCapabilityTag.READ_EXTERNAL not in tags:
            raise InvalidSourceDescriptorError(
                f"SourceRegistry: descriptor {descriptor.id!r} must "
                "include READ_EXTERNAL in tags"
            )
        forbidden = tags & _SOURCE_FORBIDDEN_TAGS
        if forbidden:
            names = sorted(t.value for t in forbidden)
            raise InvalidSourceDescriptorError(
                f"SourceRegistry: descriptor {descriptor.id!r} carries "
                f"forbidden write-side tags {names} — sources are "
                "read-only by contract"
            )


# Tags any one of which makes an emit descriptor valid (must
# carry a side effect — see design §5.4 + descriptors/emit.py).
_EMIT_REQUIRED_ANY_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.WRITE_EXTERNAL,
    }
)


class EmitRegistry(_BaseRegistry[EmitDescriptor]):
    """Registry of ``EmitDescriptor`` keyed by ``id``."""

    descriptor_type = EmitDescriptor
    unknown_error = UnknownEmitError

    def _key_of(self, descriptor: EmitDescriptor) -> str:
        return descriptor.id

    def _validate(self, descriptor: EmitDescriptor) -> None:
        tags = set(getattr(descriptor, "tags", set()) or set())
        if not (tags & _EMIT_REQUIRED_ANY_TAGS):
            raise InvalidEmitDescriptorError(
                f"EmitRegistry: descriptor {descriptor.id!r} must carry "
                "SEND_MESSAGE or WRITE_EXTERNAL"
            )


# ---------------------------------------------------------------------------
# Module-level singletons.
#
# Production code populates these once at boot (in a later
# phase). Tests should construct fresh instances rather than
# mutating the singletons, to keep the test pool independent.
# ---------------------------------------------------------------------------


TOOLS: ToolRegistry = ToolRegistry()
SOURCES: SourceRegistry = SourceRegistry()
EMITS: EmitRegistry = EmitRegistry()


__all__ = [
    "DuplicateDescriptorError",
    "UnknownToolError",
    "UnknownSourceError",
    "UnknownEmitError",
    "InvalidSourceDescriptorError",
    "InvalidEmitDescriptorError",
    "FAIL_SAFE_UNKNOWN_TAGS",
    "ToolRegistry",
    "SourceRegistry",
    "EmitRegistry",
    "TOOLS",
    "SOURCES",
    "EMITS",
]
