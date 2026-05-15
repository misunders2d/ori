"""V2 scheduler — registry cache exceptions.

Five subclasses of :class:`RegistryCacheError` cover the failure
surface that phase 7 (typed authoring tools) will encounter when
looking up Slack channels / Google Sheets / Google Docs via the
v2 registry cache.

Phase 6 slice 1 per ``docs/PHASE_6_PLAN.md`` §3.1.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7
- ``docs/PHASE_6_PLAN.md`` §3.1
"""

from __future__ import annotations


class RegistryCacheError(Exception):
    """Base for every registry-cache failure.

    Phase 6 ships only the type surface. Phase 7 (authoring
    tools) is the caller that maps each subclass to a
    user-facing message; nothing in phase 6 catches its own
    exceptions.
    """


class NoCacheAvailable(RegistryCacheError):
    """Cache for the requested kind is not available.

    Two call paths surface this:

    - :func:`app.v2.registry_cache.loader.load_cache` callers
      that want to raise rather than treat ``None`` as
      "absent" pass the on-disk path via ``path`` so the
      message is forensically useful.
    - :func:`app.v2.registry_cache.resolver.resolve_*` raises
      with ``path=None`` when the caller passed ``cache=None``;
      the resolver has no file path to report. The message
      degrades to ``not provided`` so reviewers reading a log
      can tell the two cases apart without diffing the call
      site.
    """

    def __init__(self, kind: str, path: str | None = None) -> None:
        if path is None:
            super().__init__(
                f"registry cache for kind={kind!r} not provided"
            )
        else:
            super().__init__(
                f"registry cache for kind={kind!r} missing at {path}"
            )
        self.kind = kind
        self.path = path


class CacheMiss(RegistryCacheError):
    """Cache loaded successfully but the requested id / name
    was not present in the entries.

    Distinct from :class:`NoCacheAvailable` so authoring tools
    can suggest a refresh rather than blame missing on-disk
    state.
    """

    def __init__(self, kind: str, lookup: str) -> None:
        super().__init__(
            f"id/name {lookup!r} not in cached {kind} list"
        )
        self.kind = kind
        self.lookup = lookup


class WorkspaceMismatch(RegistryCacheError):
    """Cached ``workspace_id`` / ``account_id`` differs from
    the expected one.

    The loader collapses such a cache to ``None``; this class
    exists so a refresh path that re-fetches and re-saves can
    surface the cause to the operator.
    """

    def __init__(self, kind: str, expected: str, found: str) -> None:
        super().__init__(
            f"{kind} cache workspace mismatch: "
            f"expected={expected!r}, found={found!r}"
        )
        self.kind = kind
        self.expected = expected
        self.found = found


class NoCacheAndNetworkDown(RegistryCacheError):
    """Authoring tried to resolve, no cache existed AND a
    fresh fetch failed.

    Phase 6 ships the class; the authoring layer (phase 7) is
    the call site that catches a :class:`NoCacheAvailable` /
    refresh-failure pair and re-raises this composite.
    """

    def __init__(self, kind: str, network_error: str) -> None:
        super().__init__(
            f"no cached {kind} listing and refresh failed: "
            f"{network_error}"
        )
        self.kind = kind
        self.network_error = network_error


class ChannelAmbiguous(RegistryCacheError):
    """Lookup by name matched more than one ACTIVE Slack
    channel.

    IDs stay canonical; the authoring layer should re-prompt
    the user for the explicit id. Phase 6 raises this; phase 7
    owns the user-facing rewording.
    """

    def __init__(self, name: str, candidate_ids: list[str]) -> None:
        super().__init__(
            f"channel name {name!r} matches {len(candidate_ids)} "
            f"active entries: {candidate_ids!r}"
        )
        self.name = name
        self.candidate_ids = candidate_ids


__all__ = [
    "CacheMiss",
    "ChannelAmbiguous",
    "NoCacheAndNetworkDown",
    "NoCacheAvailable",
    "RegistryCacheError",
    "WorkspaceMismatch",
]
