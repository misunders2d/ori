"""v2 scheduler — ``source_literal`` loader.

Phase 10 slice 2 per ``docs/PHASE_10_PLAN.md`` §1.1 +
``docs/CONTRACTS_V2_DESIGN.md`` §5.3 (LiteralText).

The lowest-risk source: frozen user / prompt bytes
supplied verbatim in the input args, **zero I/O**. No
network, no filesystem, no transport DI. ``as_of_datetime``
is irrelevant (a literal is already frozen); ``clock`` is
used only to stamp ``fetched_at``.

Arg contract — ``args``:
- ``source_id`` (str, required) — the InputSpec id this
  result is bound to. The slice-8 resolver injects this
  (``args = {**input.args, "source_id": input.id}``); for
  the build-the-layer unit tests callers pass it
  explicitly. (The slice-1 ``SourceLoader`` protocol is
  ``load(*, args, as_of_datetime, clock)`` — frozen +
  codex-approved — so ``source_id`` rides in ``args``
  rather than reworking the protocol.)
- ``text`` (str, required, non-empty) — the literal
  content. Stored VERBATIM (§3.6 text rule: utf-8, no
  normalisation; trailing whitespace / CRLF / BOM
  preserved). ``selection_method`` is ``content_hash``
  (design §5.3.4: plain text → normalized content hash).

A malformed arg bag raises :class:`SourceParseError`
(non-fallback per §3.5 — a misconfigured literal must
never serve a stale cached snapshot; and a literal has no
"transient" failure mode).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.registry import SOURCES, SourceRegistry
from app.v2.sources.contract import (
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import SourceParseError
from app.v2.sources.registry import (
    SOURCE_LOADERS,
    SourceLoaderRegistry,
    register_source_loader,
)
from app.v2.tool_tags import ToolCapabilityTag


SOURCE_LITERAL_ID = "source_literal"
_LITERAL_CANONICAL_KIND = "text"

#: ``READ_EXTERNAL`` is required on EVERY ``SourceDescriptor``
#: (the read-only source contract tag, design §5.3.6 /
#: registry ``_validate``). ``source_literal`` performs no
#: external read, but the tag is the source-layer membership
#: marker, not a literal capability claim.
LITERAL_DESCRIPTOR = SourceDescriptor(
    id=SOURCE_LITERAL_ID,
    description=(
        "Frozen literal text supplied verbatim in the input "
        "args — zero I/O; the content is never fetched."
    ),
    tags={ToolCapabilityTag.READ_EXTERNAL},
    supports_versioning=False,
    supported_selection_methods=[SelectionMethod.CONTENT_HASH],
)


def _require_str_arg(args: dict[str, Any], name: str) -> str:
    if name not in args:
        raise SourceParseError(
            f"source_literal requires args['{name}']",
            code="source_literal_args_invalid",
        )
    v = args[name]
    if not isinstance(v, str) or not v:
        raise SourceParseError(
            f"source_literal args['{name}'] must be a "
            "non-empty str",
            code="source_literal_args_invalid",
        )
    return v


class LiteralSource:
    """:class:`app.v2.sources.contract.SourceLoader` impl
    for frozen literal text. Stateless; zero I/O."""

    descriptor: SourceDescriptor = LITERAL_DESCRIPTOR

    async def load(
        self,
        *,
        args: dict[str, Any],
        as_of_datetime: Optional[datetime],  # noqa: ARG002 - irrelevant for a frozen literal
        clock: Callable[[], datetime],
    ) -> SourceResult:
        source_id = _require_str_arg(args, "source_id")
        text = _require_str_arg(args, "text")

        content_bytes = canonical_bytes(_LITERAL_CANONICAL_KIND, text)
        return SourceResult(
            content=text,
            content_bytes=content_bytes,
            source_kind=SOURCE_LITERAL_ID,
            source_id=source_id,
            fetched_at=clock(),
            content_hash=content_hash_for(content_bytes),
            item_count=1,
            source_version=None,
            selection_method=SelectionMethod.CONTENT_HASH,
        )


#: Module-level stateless instance — safe to share.
literal_source = LiteralSource()


def register_literal(
    *,
    sources: SourceRegistry = SOURCES,
    loaders: SourceLoaderRegistry = SOURCE_LOADERS,
) -> None:
    """Register ``source_literal`` into the descriptor +
    loader registries. Default args target the production
    singletons; tests pass fresh registries."""
    register_source_loader(
        literal_source, sources=sources, loaders=loaders
    )


__all__ = [
    "SOURCE_LITERAL_ID",
    "LITERAL_DESCRIPTOR",
    "LiteralSource",
    "literal_source",
    "register_literal",
]
