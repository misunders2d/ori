"""v2 scheduler — source loader return contract + canonical bytes.

Phase 10 slice 1 per ``docs/PHASE_10_PLAN.md`` §3.1 + §3.6
(codex plan-review round-1 #4 + round-2 #5).

Every loader returns a :class:`SourceResult`. The
``content_hash`` and the on-disk ``<source_id>.bin``
snapshot (slice 4) are derived from ``content_bytes`` —
the §3.6 canonical encoding — NEVER from re-serializing
``content``. So the hash is byte-stable regardless of
which serializer a downstream reader uses, and
``sha256(open(path,"rb").read())`` re-verifies it with
zero parsing.

§3.6 canonical-bytes rules (the ONLY thing hashed /
written):

==================  ===========================================
declared kind       canonical bytes
==================  ===========================================
text / markdown     raw bytes / utf-8 of the str, VERBATIM,
                    no normalisation (trailing ws / CRLF / BOM
                    preserved — §5.3.7)
yaml                raw bytes verbatim (round-tripping YAML is
                    lossy; treat as opaque text)
json / dict / list  ``json.dumps(obj, sort_keys=True,
                    separators=(",",":"), ensure_ascii=False)``
                    utf-8 — canonical; semantically-equal
                    payloads hash identically
binary (opt-in)     the raw bytes
==================  ===========================================
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Optional,
    Protocol,
    runtime_checkable,
)

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.types import JsonValue

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.sources.errors import SourceParseError


_CONTENT_HASH_PREFIX = "sha256:"

# kinds whose canonical bytes are the raw/utf-8 verbatim form
_VERBATIM_KINDS = frozenset({"text", "markdown", "yaml"})
# kinds canonicalised through sorted-key JSON
_JSON_KINDS = frozenset({"json", "dict", "list"})
_BINARY_KIND = "binary"

_CANONICAL_KINDS = _VERBATIM_KINDS | _JSON_KINDS | {_BINARY_KIND}


def canonical_bytes(kind: str, content: Any) -> bytes:
    """Return the §3.6 canonical byte encoding for ``content``
    under the declared ``kind``.

    This is the ONLY artifact hashed (``content_hash``) and
    written to disk (``<source_id>.bin``, slice 4). A type
    mismatch for the declared kind raises
    :class:`SourceParseError` (non-fallback — a malformed
    payload is not a transient fetch failure and must never
    serve a stale cached snapshot).
    """
    if kind in _VERBATIM_KINDS:
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        if isinstance(content, str):
            # Verbatim: encode utf-8, NO normalisation. A
            # trailing-newline delta MUST change the hash.
            return content.encode("utf-8")
        raise SourceParseError(
            f"{kind!r} content must be str or bytes, got "
            f"{type(content).__name__}",
            code="canonical_kind_type_mismatch",
        )
    if kind in _JSON_KINDS:
        try:
            return json.dumps(
                content,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SourceParseError(
                f"{kind!r} content is not JSON-serialisable: "
                f"{exc}",
                code="canonical_json_unserialisable",
            ) from exc
    if kind == _BINARY_KIND:
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        raise SourceParseError(
            "binary content must be bytes, got "
            f"{type(content).__name__}",
            code="canonical_kind_type_mismatch",
        )
    raise SourceParseError(
        f"unknown canonical kind: {kind!r} (expected one of "
        f"{sorted(_CANONICAL_KINDS)})",
        code="canonical_kind_unknown",
    )


def content_hash_for(content_bytes: bytes) -> str:
    """``"sha256:" + sha256(content_bytes).hexdigest()`` — the
    sole hash function. Re-derivable from the on-disk
    ``.bin`` file by ``content_hash_for(open(p,"rb").read())``.
    """
    return _CONTENT_HASH_PREFIX + hashlib.sha256(content_bytes).hexdigest()


class SourceResult(BaseModel):
    """A loader's typed return value.

    ``content`` is the JSON-shaped value the resolver places
    in the fire's state dict (binary carries a ``{"_b64":
    ...}`` envelope). ``content_bytes`` is the §3.6 canonical
    encoding and is the ONLY thing hashed / written — the
    in-memory ``content`` envelope is never hashed.
    """

    model_config = ConfigDict(extra="forbid")

    content: JsonValue
    content_bytes: bytes
    source_kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    fetched_at: datetime
    content_hash: str
    item_count: int = Field(ge=0)
    source_version: Optional[str] = None
    selection_method: SelectionMethod

    @field_validator("fetched_at")
    @classmethod
    def _fetched_at_tz_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "SourceResult.fetched_at must be tz-aware "
                "(UTC); naive datetime rejected."
            )
        return v.astimezone(timezone.utc)

    @field_validator("content_hash")
    @classmethod
    def _content_hash_shape(cls, v: str) -> str:
        if not v.startswith(_CONTENT_HASH_PREFIX):
            raise ValueError(
                "SourceResult.content_hash must be "
                f"'{_CONTENT_HASH_PREFIX}<64-hex>'"
            )
        hexpart = v[len(_CONTENT_HASH_PREFIX):]
        if len(hexpart) != 64 or any(
            c not in "0123456789abcdef" for c in hexpart
        ):
            raise ValueError(
                "SourceResult.content_hash must be "
                f"'{_CONTENT_HASH_PREFIX}' + 64 lowercase "
                "hex chars"
            )
        return v


@runtime_checkable
class SourceLoader(Protocol):
    """A registered read-only source loader.

    Implementations are stateless w.r.t. wall-clock / uuid
    (injected ``clock``; no ``datetime.now`` / ``uuid.uuid4``
    — phase-9 hard rule carries forward) and take no vendor
    SDK at module load (transport DI lands per loader in
    slices 6-7). A loader signals failure ONLY by raising a
    :class:`app.v2.sources.errors.SourceError` subclass —
    never a bare ``Exception`` / ``return None``.
    """

    descriptor: SourceDescriptor

    async def load(
        self,
        *,
        args: dict[str, Any],
        as_of_datetime: Optional[datetime],
        clock: Callable[[], datetime],
    ) -> SourceResult:
        ...


__all__ = [
    "SourceResult",
    "SourceLoader",
    "canonical_bytes",
    "content_hash_for",
]
