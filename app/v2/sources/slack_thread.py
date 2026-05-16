"""v2 scheduler — ``source_slack_thread`` loader.

Phase 10 slice 6 per ``docs/PHASE_10_PLAN.md`` §1.1 / §3.5
+ Q3 (Protocol DI for the read-side transport, mirroring
phase-9's emit-side ``SlackProtocol``).

Reads the messages of a Slack thread. The Slack transport
is injected as a :class:`SlackThreadReader` Protocol — NO
``slack_sdk`` / vendor import in the loader path (phase-6/9
carry-forward; the real httpx/SDK client lives outside the
source layer and is wired in a later phase).

Reader contract — mirrors phase-9 ``SlackProtocol`` (the
adapter NEVER raises for an expected Slack error; it
returns a structured mapping):

    {"ok": True,  "messages": [ <msg-dict>, ... ]}
    {"ok": False, "error": "<slack error code>"}

The loader maps that into the §3.5 typed taxonomy
(slice-5 semantics: a non-fallback error bypasses cache +
fallback):
- auth (``not_authed`` / ``invalid_auth`` /
  ``account_inactive`` / ``token_revoked`` /
  ``token_expired`` / ``no_permission`` /
  ``missing_scope``) → :class:`SourceAuthError`
  (NON-fallback).
- transient (``ratelimited`` / ``service_unavailable`` /
  ``internal_error`` / ``fatal_error`` /
  ``request_timeout``) OR a transport-level exception
  raised by the reader → :class:`SourceFetchError`
  (fallback-eligible).
- source-missing (``channel_not_found`` /
  ``thread_not_found`` / ``not_in_channel``) →
  :class:`SourceFetchError` (design §5.3.2 lists "source
  missing" under ``fallback_policy``).
- malformed reader response (missing/!list ``messages``,
  unrenderable) → :class:`SourceParseError` (NON-fallback).
- ``slack_reader_unconfigured`` (the safe-default
  singleton has no real reader yet) →
  :class:`SourcePolicyError` (NON-fallback — fails safe;
  never serves a stale snapshot until a reader is wired).
- any OTHER ``ok:false`` error → :class:`SourceFetchError`
  (conservative: treat unknown as a retryable fetch
  failure, never silently auth-bypass).

Arg contract — ``args``:
- ``source_id`` (str, required) — rides in args (slice-1
  ``SourceLoader`` protocol frozen; slice-8 resolver
  injects it).
- ``channel`` (str, required) — Slack channel id.
- ``thread_ts`` (str, required) — root message ts of the
  thread.

Build-the-layer: NOT wired into the worker fire path.
"""

from __future__ import annotations

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


SOURCE_SLACK_THREAD_ID = "source_slack_thread"

_AUTH_ERRORS = frozenset(
    {
        "not_authed",
        "invalid_auth",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "no_permission",
        "missing_scope",
    }
)
_TRANSIENT_ERRORS = frozenset(
    {
        "ratelimited",
        "service_unavailable",
        "internal_error",
        "fatal_error",
        "request_timeout",
    }
)
_SOURCE_MISSING_ERRORS = frozenset(
    {
        "channel_not_found",
        "thread_not_found",
        "not_in_channel",
    }
)


@runtime_checkable
class SlackThreadReader(Protocol):
    """Read-side Slack transport (Protocol DI). Mirrors the
    phase-9 emit-side ``SlackProtocol``: NEVER raises for an
    expected Slack error — returns the structured mapping
    documented in the module docstring. A transport-level
    failure (network / unexpected) MAY raise; the loader
    wraps it as :class:`SourceFetchError`."""

    async def read_thread(
        self, *, channel: str, thread_ts: str
    ) -> Mapping[str, Any]:
        ...


class _UnconfiguredSlackThreadReader:
    """Safe default for the production singleton until a
    real Slack reader is wired (later phase). Always
    returns the ``slack_reader_unconfigured`` sentinel →
    the loader maps it to a NON-fallback
    :class:`SourcePolicyError` so a fire fails safe (never
    serves a stale snapshot)."""

    async def read_thread(
        self, *, channel: str, thread_ts: str
    ) -> Mapping[str, Any]:
        return {"ok": False, "error": "slack_reader_unconfigured"}


SLACK_THREAD_DESCRIPTOR = SourceDescriptor(
    id=SOURCE_SLACK_THREAD_ID,
    description=(
        "Read the messages of a Slack thread via an "
        "injected read-only Slack transport (Protocol DI; "
        "no vendor SDK in the loader path)."
    ),
    tags={ToolCapabilityTag.READ_EXTERNAL},
    supports_versioning=False,
    supported_selection_methods=[SelectionMethod.CONTENT_HASH],
)


def _req_str(args: dict[str, Any], name: str) -> str:
    if name not in args:
        raise SourceParseError(
            f"source_slack_thread requires args['{name}']",
            code="source_slack_thread_args_invalid",
        )
    v = args[name]
    if not isinstance(v, str) or not v:
        raise SourceParseError(
            f"source_slack_thread args['{name}'] must be a "
            "non-empty str",
            code="source_slack_thread_args_invalid",
        )
    return v


def _raise_for_slack_error(error: str) -> None:
    """Map a Slack ``ok:false`` error code into the §3.5
    typed taxonomy. Always raises."""
    if error in _AUTH_ERRORS:
        raise SourceAuthError(
            f"slack thread read auth failure: {error}",
            code=f"slack_{error}",
        )
    if error == "slack_reader_unconfigured":
        raise SourcePolicyError(
            "source_slack_thread has no configured Slack "
            "reader (safe default — fails closed)",
            code="slack_reader_unconfigured",
        )
    if error in _TRANSIENT_ERRORS or error in _SOURCE_MISSING_ERRORS:
        raise SourceFetchError(
            f"slack thread read failed (retryable): {error}",
            code=f"slack_{error}",
        )
    # Unknown error code → conservative transient (never a
    # silent auth bypass; never a non-fallback masking).
    raise SourceFetchError(
        f"slack thread read failed: {error}",
        code=f"slack_{error}",
    )


class SlackThreadSource:
    """:class:`app.v2.sources.contract.SourceLoader` impl
    for a Slack thread, via an injected
    :class:`SlackThreadReader`."""

    descriptor: SourceDescriptor = SLACK_THREAD_DESCRIPTOR

    def __init__(self, *, reader: SlackThreadReader) -> None:
        self._reader = reader

    async def load(
        self,
        *,
        args: dict[str, Any],
        as_of_datetime: Optional[datetime],  # noqa: ARG002 - Slack has no as-of dimension here
        clock: Callable[[], datetime],
    ) -> SourceResult:
        source_id = _req_str(args, "source_id")
        channel = _req_str(args, "channel")
        thread_ts = _req_str(args, "thread_ts")

        try:
            resp = await self._reader.read_thread(
                channel=channel, thread_ts=thread_ts
            )
        except Exception as exc:  # transport-level failure
            raise SourceFetchError(
                f"slack thread read transport error: {exc}",
                code="slack_transport_error",
            ) from exc

        if not isinstance(resp, Mapping):
            raise SourceParseError(
                "slack reader returned a non-mapping "
                f"response: {type(resp).__name__}",
                code="slack_response_malformed",
            )
        if resp.get("ok") is not True:  # strict identity (phase-9 parity)
            error = resp.get("error")
            _raise_for_slack_error(
                error if isinstance(error, str) else "unknown_error"
            )

        messages = resp.get("messages")
        if not isinstance(messages, list):
            raise SourceParseError(
                "slack reader 'ok' response missing a list "
                "'messages' field",
                code="slack_response_malformed",
            )

        try:
            content_bytes = canonical_bytes("list", messages)
        except SourceParseError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SourceParseError(
                f"slack thread messages not canonicalisable: "
                f"{exc}",
                code="slack_response_malformed",
            ) from exc

        return SourceResult(
            content=messages,
            content_bytes=content_bytes,
            source_kind=SOURCE_SLACK_THREAD_ID,
            source_id=source_id,
            fetched_at=clock(),
            content_hash=content_hash_for(content_bytes),
            item_count=len(messages),
            source_version=None,
            selection_method=SelectionMethod.CONTENT_HASH,
        )


#: Production singleton — safe default reader (fails
#: closed) until a real Slack reader is wired later.
slack_thread_source = SlackThreadSource(
    reader=_UnconfiguredSlackThreadReader()
)


def register_slack_thread(
    *,
    sources: SourceRegistry = SOURCES,
    loaders: SourceLoaderRegistry = SOURCE_LOADERS,
) -> None:
    """Register ``source_slack_thread`` (atomic paired
    registration). Default args target the production
    singletons; tests pass fresh registries."""
    register_source_loader(
        slack_thread_source, sources=sources, loaders=loaders
    )


__all__ = [
    "SOURCE_SLACK_THREAD_ID",
    "SLACK_THREAD_DESCRIPTOR",
    "SlackThreadReader",
    "SlackThreadSource",
    "slack_thread_source",
    "register_slack_thread",
]
