"""Amazon Ads MCP — auth/header bridge for the ADK ``McpToolset``.

This is the agent-side *bridge* between Ori and Amazon's **hosted, remote**
Amazon Ads MCP server (Streamable HTTP transport). It is NOT a subprocess /
stdio proxy — ADK's ``McpToolset`` connects to the remote endpoint directly;
this module only supplies the per-session HTTP headers it needs.

Responsibilities
----------------
1. Read the LWA app credentials from the vault (``deploy/vault.py`` API only —
   never the raw ``data/vault/credentials.json``; see ``docs/AI_EDITS.md`` §3).
2. Mint a short-lived LWA *access token* from the long-lived
   ``ADS_API_REFRESH_TOKEN`` and cache it until ~60 s before expiry.
3. Expose ``header_provider(readonly_context)`` — the callable ADK's
   ``McpToolset(header_provider=...)`` invokes once per MCP session to obtain:

       Amazon-Ads-ClientId: <client id>
       Authorization:       Bearer <access token>
       Accept:              application/json, text/event-stream

4. Resolve the region-specific MCP endpoint URL.

Account context
---------------
Default = **dynamic**: only the two required headers are sent; account
identifiers (profileId / advertiserAccountId / managerAccountId) travel in
the tool request body and the model/server negotiates them per call.

Optional **fixed** context: set ``ADS_API_ACCOUNT_SELECTION_MODE=FIXED`` in
the vault plus at least one of ``ADS_API_DEFAULT_PROFILE_ID`` /
``ADS_API_DEFAULT_ACCOUNT_ID`` / ``ADS_API_DEFAULT_MANAGER_ACCOUNT_ID``. All
fixed identifiers must belong to the same account (Amazon requirement).

Rule 13 (nothing fails silently): every failure path logs at ``error`` /
``critical`` and raises ``AmazonAdsAuthError`` so the calling tool surfaces
the verbatim message to the agent instead of degrading to silent 401s.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

from deploy import vault

logger = logging.getLogger(__name__)

# LWA token endpoint (global — not region-specific).
_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# Amazon-hosted remote Ads MCP endpoints. Region is NOT agnostic — a profile
# only resolves on the endpoint for its region.
_REGION_MCP_URL = {
    "NA": "https://advertising-ai.amazon.com/mcp",
    "EU": "https://advertising-ai-eu.amazon.com/mcp",
    "FE": "https://advertising-ai-fe.amazon.com/mcp",
}
_DEFAULT_REGION = "NA"

# Refresh the access token this many seconds before its stated expiry so an
# in-flight request never races the boundary.
_EXPIRY_SKEW_SECONDS = 60


class AmazonAdsAuthError(RuntimeError):
    """Raised when the Amazon Ads LWA token cannot be minted/refreshed.

    Surfaced verbatim to the agent (Rule 13) — never swallowed.

    Flags steer how the bot should react:

    * ``reauth_required`` — the refresh token is missing or definitively
      rejected by LWA (``invalid_grant`` / ``invalid_client`` / 401 / 403).
      ONLY then should the bot ask the user to re-authorize.
    * ``transient`` — a temporary failure (429 / 5xx / network). The token
      may still be valid; the background refresher retries automatically.
      The bot must NOT prompt for re-authorization.
    """

    def __init__(
        self,
        message: str,
        *,
        reauth_required: bool = False,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.reauth_required = reauth_required
        self.transient = transient


class _AccessTokenCache:
    """Process-wide cached LWA access token.

    Loop-safety contract (reviewer fix #3): the blocking LWA HTTP exchange
    must NEVER run on the asyncio event loop. ``get()`` is pure in-memory —
    it never does network and raises if the token is cold/stale. The token
    is kept warm two ways, both off-loop:

    * ``ensure_warm()`` — synchronous mint, called from ``asyncio.to_thread``
      in ``AmazonAdsToolset.get_tools`` (an async context) BEFORE any MCP
      session opens, so the first ``header_provider`` call already has a
      token.
    * a daemon refresher thread — re-mints ~60 s before expiry so long
      sessions that cross the 1 h boundary never force a mint on the loop.

    ``header_provider`` therefore only ever reads memory.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Bumped every time a NEW token value is stored. Consumers (the
        # toolset) watch this to know when a rebuild is actually warranted
        # instead of rebuilding every turn.
        self._token_generation: int = 0
        # Refresher cohort tracking. Each refresher thread owns its own stop
        # Event and a generation id; superseding bumps the id and sets the
        # old Event so exactly one daemon survives an invalidate/ensure_warm
        # cycle (reviewer fix #2 — no shared-event leak).
        self._refresher: threading.Thread | None = None
        self._refresher_gen: int = 0
        self._stop = threading.Event()

    def get(self) -> str:
        """Return a currently-valid token from memory, or raise. No network.

        Never call the LWA endpoint here — this runs inside ADK's async
        ``header_provider`` path and a blocking POST would stall the whole
        event loop. ``ensure_warm`` + the refresher keep this populated.
        """
        token = self._token
        if token and time.time() < self._expires_at:
            return token
        msg = (
            "Amazon Ads access token is cold/expired and the off-loop "
            "refresher has not produced one. Refusing to mint on the event "
            "loop (would stall it). This indicates ensure_warm() was not "
            "called or the LWA refresh is failing — check earlier logs."
        )
        logger.error(msg)
        raise AmazonAdsAuthError(msg)

    @property
    def token_generation(self) -> int:
        """Monotone counter — changes only when the token value rotates.

        The toolset rebuilds its inner ``McpToolset`` iff this differs from
        the generation it last built with (or it has none), so steady-state
        turns reuse the same session instead of re-handshaking.
        """
        return self._token_generation

    def _store(self, token: str, expires_in: int) -> None:
        with self._lock:
            changed = token != self._token
            self._token = token
            self._expires_at = time.time() + max(
                0, expires_in - _EXPIRY_SKEW_SECONDS
            )
            if changed:
                self._token_generation += 1

    def ensure_warm(self) -> None:
        """Synchronous mint if cold. MUST be called off the event loop
        (e.g. via ``asyncio.to_thread``). Idempotent / cheap when warm."""
        if self._token and time.time() < self._expires_at:
            return
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return
            token, expires_in = _mint_access_token()
        self._store(token, expires_in)
        self._start_refresher()

    def _start_refresher(self) -> None:
        if self._refresher and self._refresher.is_alive():
            return
        # Open a fresh cohort: bump the generation and hand the new thread
        # its OWN stop Event. Any prior cohort's Event is set so it exits
        # even if it never observed a stop before being superseded.
        with self._lock:
            self._refresher_gen += 1
            gen = self._refresher_gen
            old_stop = self._stop
            self._stop = threading.Event()
            stop = self._stop
        old_stop.set()
        t = threading.Thread(
            target=self._refresh_loop,
            args=(gen, stop),
            name=f"amazon-ads-token-refresher-{gen}",
            daemon=True,
        )
        self._refresher = t
        t.start()

    def _refresh_loop(self, gen: int, stop: threading.Event) -> None:
        # Daemon: dies with the process. Owns `stop` for its cohort only.
        # Exits if signalled OR if a newer cohort superseded it (gen check) —
        # guarantees exactly one live refresher (Rule 13: failures logged,
        # never a silent exit).
        while True:
            if self._refresher_gen != gen:
                return
            wait_for = max(30.0, self._expires_at - time.time())
            if stop.wait(timeout=wait_for):
                return
            if self._refresher_gen != gen:
                return
            try:
                token, expires_in = _mint_access_token()
                self._store(token, expires_in)
                logger.info("Amazon Ads token proactively refreshed.")
            except Exception as exc:  # noqa: BLE001
                # Keep the (now stale) token; retry soon. get() will raise
                # loudly if it actually goes stale before we recover.
                logger.error(
                    "Amazon Ads token background refresh failed: %r — "
                    "retrying in 30 s.",
                    exc,
                )
                if stop.wait(timeout=30.0):
                    return

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0
            # Retire the current cohort: bump gen + signal its Event so the
            # running daemon (if any) exits promptly and cannot leak.
            self._refresher_gen += 1
            self._stop.set()
        self._refresher = None


_token_cache = _AccessTokenCache()


def _require(key: str) -> str:
    """Read a required vault key or raise (Rule 13 — loud, never silent)."""
    val = vault.get(key, "").strip()
    if not val:
        msg = (
            f"Amazon Ads MCP misconfigured: vault key {key!r} is missing or "
            f"empty. Run `uv run python scripts/ads_oauth_helper.py` to mint "
            f"ADS_API_REFRESH_TOKEN, and ensure ADS_API_CLIENT_ID / "
            f"ADS_API_CLIENT_SECRET are populated."
        )
        logger.error(msg)
        raise AmazonAdsAuthError(msg, reauth_required=True)
    return val


def credentials_present() -> bool:
    """True iff all three LWA secrets are in the vault.

    Used by the toolset to decide between (a) wiring the remote MCP and
    (b) degrading to an empty toolset with a CRITICAL log — without raising
    during agent assembly.
    """
    return all(
        vault.get(k, "").strip()
        for k in ("ADS_API_CLIENT_ID", "ADS_API_CLIENT_SECRET", "ADS_API_REFRESH_TOKEN")
    )


def resolve_region() -> str:
    """Region code (NA/EU/FE) from the vault, defaulting to NA."""
    region = vault.get("ADS_API_REGION", _DEFAULT_REGION).strip().upper()
    if region not in _REGION_MCP_URL:
        logger.error(
            "Unknown ADS_API_REGION=%r — falling back to %s. Valid: %s",
            region,
            _DEFAULT_REGION,
            ", ".join(_REGION_MCP_URL),
        )
        return _DEFAULT_REGION
    return region


def mcp_url() -> str:
    """Region-specific Amazon Ads MCP endpoint URL."""
    return _REGION_MCP_URL[resolve_region()]


def _mint_access_token() -> tuple[str, int]:
    """Exchange the refresh token for a fresh LWA access token.

    Returns ``(access_token, expires_in_seconds)``. Raises
    ``AmazonAdsAuthError`` on any failure (logged first).
    """
    client_id = _require("ADS_API_CLIENT_ID")
    client_secret = _require("ADS_API_CLIENT_SECRET")
    refresh_token = _require("ADS_API_REFRESH_TOKEN")

    try:
        resp = httpx.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        # Network/timeout = transient. Token may still be valid; the
        # refresher retries. Do NOT prompt re-authorization.
        msg = (
            "Amazon Ads LWA token request failed (network/timeout) — "
            "temporary, automatic retry in progress; no re-authorization "
            f"needed: {type(exc).__name__}"
        )
        logger.error(msg)
        raise AmazonAdsAuthError(msg, transient=True) from exc

    if resp.status_code != 200:
        # Classify WITHOUT logging the body verbatim (can echo the secret).
        # Parse only the LWA `error` code.
        err_code = ""
        try:
            err_code = str(resp.json().get("error", "")).lower()
        except Exception:
            err_code = ""
        sc = resp.status_code
        definitive_reauth = (
            err_code in ("invalid_grant", "invalid_client", "unauthorized_client")
            or sc in (401, 403)
        )
        if definitive_reauth:
            msg = (
                f"Amazon Ads LWA rejected the refresh token "
                f"(HTTP {sc}/{err_code or 'auth'}). Re-authorization is "
                f"required — run `uv run python scripts/ads_oauth_helper.py`."
            )
            logger.error(msg)
            raise AmazonAdsAuthError(msg, reauth_required=True)
        if sc == 429 or sc >= 500:
            msg = (
                f"Amazon Ads LWA temporary failure (HTTP {sc}) — automatic "
                f"retry in progress; no re-authorization needed."
            )
            logger.error(msg)
            raise AmazonAdsAuthError(msg, transient=True)
        # Other 4xx (e.g. invalid_request): a real config/contract problem,
        # not a token-revocation. Surface loudly, but not as reauth.
        msg = (
            f"Amazon Ads LWA token exchange failed (HTTP {sc}/"
            f"{err_code or 'unknown'}). Check ADS_API_CLIENT_ID/SECRET "
            f"configuration."
        )
        logger.error(msg)
        raise AmazonAdsAuthError(msg)

    payload = resp.json()
    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not access_token or not isinstance(expires_in, int):
        msg = (
            "Amazon Ads LWA token endpoint returned no usable access_token / "
            "expires_in."
        )
        logger.error(msg)
        raise AmazonAdsAuthError(msg)

    logger.info("Amazon Ads LWA access token minted (expires_in=%ss).", expires_in)
    return access_token, expires_in


def _fixed_context_headers() -> dict[str, str]:
    """Optional FIXED account-context headers.

    Empty unless ``ADS_API_ACCOUNT_SELECTION_MODE=FIXED``. When FIXED, at
    least one account identifier must be configured; all must belong to the
    same account (Amazon requirement — not enforced here, operator's
    responsibility).
    """
    mode = vault.get("ADS_API_ACCOUNT_SELECTION_MODE", "").strip().upper()
    if mode != "FIXED":
        return {}

    headers: dict[str, str] = {"Amazon-Ads-AI-Account-Selection-Mode": "FIXED"}
    profile_id = vault.get("ADS_API_DEFAULT_PROFILE_ID", "").strip()
    account_id = vault.get("ADS_API_DEFAULT_ACCOUNT_ID", "").strip()
    manager_id = vault.get("ADS_API_DEFAULT_MANAGER_ACCOUNT_ID", "").strip()
    if profile_id:
        headers["Amazon-Advertising-API-Scope"] = profile_id
    if account_id:
        headers["Amazon-Ads-AccountID"] = account_id
    if manager_id:
        headers["Amazon-Ads-Manager-AccountID"] = manager_id

    if len(headers) == 1:  # only the mode header — no identifier set
        msg = (
            "ADS_API_ACCOUNT_SELECTION_MODE=FIXED but none of "
            "ADS_API_DEFAULT_PROFILE_ID / ADS_API_DEFAULT_ACCOUNT_ID / "
            "ADS_API_DEFAULT_MANAGER_ACCOUNT_ID is set. FIXED mode requires "
            "at least one account identifier."
        )
        logger.error(msg)
        raise AmazonAdsAuthError(msg)
    return headers


def header_provider(readonly_context: Any = None) -> dict[str, str]:
    """ADK ``McpToolset(header_provider=...)`` callable.

    Invoked once per MCP session. Returns the headers Amazon's remote Ads
    MCP requires. Raises ``AmazonAdsAuthError`` (logged) on any failure so
    the tool call fails loudly instead of silently 401-ing (Rule 13).

    ``readonly_context`` is accepted for ADK's calling convention but not
    used — account context is vault-driven, not session-driven.
    """
    client_id = _require("ADS_API_CLIENT_ID")
    access_token = _token_cache.get()
    headers = {
        "Amazon-Ads-ClientId": client_id,
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
    }
    headers.update(_fixed_context_headers())
    return headers
