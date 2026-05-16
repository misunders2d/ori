"""v2 scheduler — typed source-loader error contract.

Phase 10 slice 1 per ``docs/PHASE_10_PLAN.md`` §3.5
(codex plan-review round-1 #2 + round-2 #2/#3).

Fallback eligibility must be **unforgeable**. Exactly ONE
subclass — :class:`SourceFetchError` — is
``fallback_eligible``; every other :class:`SourceError`
subclass routes straight to ``SOURCE_FAILED`` with the
per-source cache AND fallback bypassed. This is the
resolver's ONLY branch (slice 8): a denied path, an
oversize refusal, an auth failure, or an unparseable body
can therefore never serve a stale cached snapshot.

``fallback_eligible`` is a :data:`typing.ClassVar` bool
that defaults to ``False`` on the base, set ``True`` ONLY
on :class:`SourceFetchError`. A hypothetical future
subclass is non-fallback **by construction** (fail-safe
default) — it cannot accidentally widen the fallback
surface.

``code`` is the stable machine string carried into the
``SOURCE_FAILED`` / ``source_failed`` event payload so
observers can grep the failure class.
"""

from __future__ import annotations

from typing import ClassVar, Optional


class SourceError(Exception):
    """Base for every source-loader failure.

    Never raised directly — loaders raise the precise
    subclass so the resolver's single ``fallback_eligible``
    rule can route the failure. Carries a machine ``code``
    for the event payload.
    """

    #: Stable machine code for the event payload. Per-class
    #: default; an instance MAY refine it via ``code=`` for
    #: a more specific payload without changing the class.
    code: ClassVar[str] = "source_error"

    #: Fail-safe default — a SourceError is NOT eligible for
    #: the per-source cache / fallback path unless a
    #: subclass explicitly opts in. Only SourceFetchError
    #: does.
    fallback_eligible: ClassVar[bool] = False

    def __init__(
        self, message: str = "", *, code: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.message = message
        # Instance-level refinement of the payload code is
        # allowed (e.g. "path_outside_allowed_root"); it
        # never changes the class taxonomy / fallback rule.
        self._instance_code = code

    @property
    def payload_code(self) -> str:
        """The code to write into the event payload — the
        instance refinement when supplied, else the class
        taxonomy code."""
        return self._instance_code or self.code


class SourceFetchError(SourceError):
    """Transient fetch failure ONLY: connection refused,
    DNS failure, 5xx, timeout, read reset.

    The SOLE ``fallback_eligible`` class — the resolver
    consults ``ref.cache.fallback_policy`` for this and
    nothing else.
    """

    code: ClassVar[str] = "source_fetch_error"
    fallback_eligible: ClassVar[bool] = True


class SourceAuthError(SourceError):
    """401 / 403 / token expired / no credentials.

    Non-fallback → ``SOURCE_FAILED``; routes to admin
    re-auth. A warm last-good snapshot is NEVER served on
    an auth failure.
    """

    code: ClassVar[str] = "source_auth_error"


class SourceSecurityError(SourceError):
    """Local-file fence rejection: path outside the
    allowlist root, deny-list hit, symlink escape, ``..``
    traversal (plan §3.2).

    Non-fallback → ``SOURCE_FAILED``. A denied read MUST
    NEVER serve a last-good cached snapshot.
    """

    code: ClassVar[str] = "source_security_error"


class SourcePolicyError(SourceError):
    """Policy refusal: oversize ``fail_and_alert``, mime
    not allow-listed, shape ``require_reapprove`` refusal
    (plan §3.4 / §3.5).

    Non-fallback → ``SOURCE_FAILED``.
    """

    code: ClassVar[str] = "source_policy_error"


class SourceParseError(SourceError):
    """Fetched bytes are unparseable for the declared kind.

    Not transient — non-fallback → ``SOURCE_FAILED``.
    """

    code: ClassVar[str] = "source_parse_error"


__all__ = [
    "SourceError",
    "SourceFetchError",
    "SourceAuthError",
    "SourceSecurityError",
    "SourcePolicyError",
    "SourceParseError",
]
