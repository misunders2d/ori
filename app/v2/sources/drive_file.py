"""v2 scheduler — ``source_drive_file`` loader.

Phase 10 slice 7 per ``docs/PHASE_10_PLAN.md`` §1.1 / §3.5
+ Q3 (Protocol DI for the read-side transport).

Reads a Google Drive file (Doc / Sheet / plain file). The
Drive transport is injected as a :class:`DriveFileReader`
Protocol — NO ``googleapiclient`` / google SDK import in
the loader path. **OAuth is NOT rolled here**: the
production reader is wired by the caller via the same
Protocol-DI credential path the registry cache already
uses (``app.v2.registry_cache.refresh.GoogleDriveClient``
is itself a Protocol whose concrete
``googleapiclient``-backed implementation lives outside
the package). The loader/source layer never touches
credentials — that is the established mechanism, reused.

Reader contract — mirrors phase-9 ``SlackProtocol`` (the
adapter NEVER raises for an expected Drive error; returns
a structured mapping; a transport-level failure MAY
raise):

    {"ok": True, "content": <str|dict|list|bytes>,
     "kind": "text"|"markdown"|"json"|"yaml"|"binary",
     "revision": <opt str>}            # Drive revisionId
    {"ok": False, "error": "<drive error code>"}

The loader maps that into the §3.5 typed taxonomy
(slice-5 semantics — a non-fallback error bypasses cache
+ fallback):
- auth / permission (``invalid_grant`` / ``unauthorized``
  / ``unauthenticated`` / ``forbidden`` /
  ``insufficientPermissions`` / ``authError`` /
  ``tokenExpired``) → :class:`SourceAuthError`
  (NON-fallback).
- transient (``rateLimitExceeded`` /
  ``userRateLimitExceeded`` / ``quotaExceeded`` /
  ``backendError`` / ``internalError`` /
  ``serviceUnavailable`` / ``timeout``) OR a
  transport-level exception → :class:`SourceFetchError`
  (fallback-eligible).
- not-found (``notFound`` / ``fileNotFound``) →
  :class:`SourceFetchError` (contained-absent / "source
  missing", design §5.3.2 — fallback-eligible).
- malformed (non-mapping, missing/!str ``kind``, unknown
  kind, content type mismatch) →
  :class:`SourceParseError` (NON-fallback).
- ``drive_reader_unconfigured`` (safe-default singleton)
  → :class:`SourcePolicyError` (NON-fallback — fails
  closed until a real reader is wired).
- any OTHER ``ok:false`` error → :class:`SourceFetchError`
  (conservative; never a silent auth bypass).

Arg contract — ``args``:
- ``source_id`` (str, required) — rides in args (slice-1
  ``SourceLoader`` protocol frozen; slice-8 resolver
  injects it).
- ``file_id`` (str, required) — Drive file id.

Build-the-layer: NOT wired into the worker fire path.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.registry import SOURCES, SourceRegistry
from app.v2.sources.contract import (
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import (
    SourceAuthError,
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
)
from app.v2.sources.registry import (
    SOURCE_LOADERS,
    SourceLoaderRegistry,
    register_source_loader,
)
from app.v2.tool_tags import ToolCapabilityTag


SOURCE_DRIVE_FILE_ID = "source_drive_file"

_CANONICAL_KINDS = frozenset(
    {"text", "markdown", "yaml", "json", "binary"}
)

# Full Drive permission/auth reason set — EVERY one maps
# to the NON-fallback SourceAuthError so an auth/permission
# failure can never serve a stale cached snapshot
# (codex slice-7 🔴). Genuine rate-limit / 5xx / network
# stay transient (SourceFetchError); not-found stays
# contained-absent.
_AUTH_ERRORS = frozenset(
    {
        # OAuth / token / authentication
        "invalid_grant",
        "unauthorized",
        "unauthenticated",
        "authError",
        "tokenExpired",
        # 403 permission-class reasons
        "forbidden",
        "insufficientPermissions",
        "permissionDenied",
        "insufficientFilePermissions",
        "appNotAuthorizedToFile",
        "domainPolicy",
        # unregistered / no-credentials use (auth-origin,
        # NOT a transient rate-limit)
        "dailyLimitExceededUnreg",
    }
)
_TRANSIENT_ERRORS = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "backendError",
        "internalError",
        "serviceUnavailable",
        "timeout",
    }
)
_NOT_FOUND_ERRORS = frozenset({"notFound", "fileNotFound"})


@runtime_checkable
class DriveFileReader(Protocol):
    """Read-side Drive transport (Protocol DI). The concrete
    ``googleapiclient``-backed implementation + its OAuth
    credentials are wired by the caller OUTSIDE this
    package — the same Protocol-DI cred path the registry
    cache's ``GoogleDriveClient`` uses. NEVER raises for an
    expected Drive error (returns the structured mapping);
    a transport-level failure MAY raise."""

    async def read_file(self, *, file_id: str) -> Mapping[str, Any]:
        ...


class _UnconfiguredDriveFileReader:
    """Safe default for the production singleton until a
    real Drive reader (with creds) is wired. Always returns
    the ``drive_reader_unconfigured`` sentinel → the loader
    maps it to a NON-fallback :class:`SourcePolicyError`
    (fails closed; never serves a stale snapshot)."""

    async def read_file(self, *, file_id: str) -> Mapping[str, Any]:
        return {"ok": False, "error": "drive_reader_unconfigured"}


DRIVE_FILE_DESCRIPTOR = SourceDescriptor(
    id=SOURCE_DRIVE_FILE_ID,
    description=(
        "Read a Google Drive file (Doc / Sheet / plain "
        "file) via an injected read-only Drive transport "
        "(Protocol DI; no google SDK in the loader path; "
        "OAuth via the established registry-cache cred "
        "path)."
    ),
    tags={
        ToolCapabilityTag.READ_EXTERNAL,
        ToolCapabilityTag.USES_OAUTH,
    },
    supports_versioning=True,
    supported_selection_methods=[SelectionMethod.STABLE_ID],
)


def _req_str(args: dict[str, Any], name: str) -> str:
    if name not in args:
        raise SourceParseError(
            f"source_drive_file requires args['{name}']",
            code="source_drive_file_args_invalid",
        )
    v = args[name]
    if not isinstance(v, str) or not v:
        raise SourceParseError(
            f"source_drive_file args['{name}'] must be a "
            "non-empty str",
            code="source_drive_file_args_invalid",
        )
    return v


def _raise_for_drive_error(error: str) -> None:
    """Map a Drive ``ok:false`` error code into the §3.5
    typed taxonomy. Always raises."""
    if error in _AUTH_ERRORS:
        raise SourceAuthError(
            f"drive read auth failure: {error}",
            code=f"drive_{error}",
        )
    if error == "drive_reader_unconfigured":
        raise SourcePolicyError(
            "source_drive_file has no configured Drive "
            "reader (safe default — fails closed)",
            code="drive_reader_unconfigured",
        )
    if error in _TRANSIENT_ERRORS or error in _NOT_FOUND_ERRORS:
        raise SourceFetchError(
            f"drive read failed (retryable): {error}",
            code=f"drive_{error}",
        )
    raise SourceFetchError(
        f"drive read failed: {error}",
        code=f"drive_{error}",
    )


class DriveFileSource:
    """:class:`app.v2.sources.contract.SourceLoader` impl
    for a Google Drive file, via an injected
    :class:`DriveFileReader`."""

    descriptor: SourceDescriptor = DRIVE_FILE_DESCRIPTOR

    def __init__(self, *, reader: DriveFileReader) -> None:
        self._reader = reader

    async def load(
        self,
        *,
        args: dict[str, Any],
        as_of_datetime: Optional[datetime],  # noqa: ARG002 - revision pin is the as-of dimension; future phase
        clock: Callable[[], datetime],
    ) -> SourceResult:
        source_id = _req_str(args, "source_id")
        file_id = _req_str(args, "file_id")

        try:
            resp = await self._reader.read_file(file_id=file_id)
        except Exception as exc:  # transport-level failure
            raise SourceFetchError(
                f"drive read transport error: {exc}",
                code="drive_transport_error",
            ) from exc

        if not isinstance(resp, Mapping):
            raise SourceParseError(
                "drive reader returned a non-mapping "
                f"response: {type(resp).__name__}",
                code="drive_response_malformed",
            )
        if resp.get("ok") is not True:  # strict identity (phase-9 parity)
            error = resp.get("error")
            _raise_for_drive_error(
                error if isinstance(error, str) else "unknown_error"
            )

        kind = resp.get("kind")
        if not isinstance(kind, str) or kind not in _CANONICAL_KINDS:
            raise SourceParseError(
                f"drive reader returned an invalid 'kind': "
                f"{kind!r} (expected one of "
                f"{sorted(_CANONICAL_KINDS)})",
                code="drive_response_malformed",
            )
        if "content" not in resp:
            raise SourceParseError(
                "drive reader 'ok' response missing "
                "'content'",
                code="drive_response_malformed",
            )
        content = resp["content"]

        # canonical_bytes raises SourceParseError on a
        # type/kind mismatch (slice-1 contract) — that is
        # the correct non-fallback classification for a
        # malformed body. content_bytes is hashed/written
        # VERBATIM; the JSON-shaped `content` field carries
        # a {"_b64": ...} envelope for binary so it stays
        # JsonValue (mirrors local_file).
        content_bytes = canonical_bytes(kind, content)
        if kind == "binary":
            content_field: Any = {
                "_b64": base64.b64encode(content).decode("ascii")
            }
        else:
            content_field = content

        revision = resp.get("revision")
        source_version = (
            revision if isinstance(revision, str) and revision else None
        )

        # item_count: a Drive file read is one item; a
        # list body still represents a single fetched
        # object (per-row series selection lands later).
        item_count = (
            len(content) if isinstance(content, list) else 1
        )

        return SourceResult(
            content=content_field,
            content_bytes=content_bytes,
            source_kind=SOURCE_DRIVE_FILE_ID,
            source_id=source_id,
            fetched_at=clock(),
            content_hash=content_hash_for(content_bytes),
            item_count=item_count,
            source_version=source_version,
            selection_method=SelectionMethod.STABLE_ID,
        )


#: Production singleton — safe default reader (fails
#: closed) until a real Drive reader + creds are wired.
drive_file_source = DriveFileSource(
    reader=_UnconfiguredDriveFileReader()
)


def register_drive_file(
    *,
    sources: SourceRegistry = SOURCES,
    loaders: SourceLoaderRegistry = SOURCE_LOADERS,
) -> None:
    """Register ``source_drive_file`` (atomic paired
    registration). Default args target the production
    singletons; tests pass fresh registries."""
    register_source_loader(
        drive_file_source, sources=sources, loaders=loaders
    )


__all__ = [
    "SOURCE_DRIVE_FILE_ID",
    "DRIVE_FILE_DESCRIPTOR",
    "DriveFileReader",
    "DriveFileSource",
    "drive_file_source",
    "register_drive_file",
]
