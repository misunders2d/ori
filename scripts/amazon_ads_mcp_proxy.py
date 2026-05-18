"""Amazon Ads MCP — stdio↔HTTP proxy with auto-rotating LWA bearer.

Claude Code (and later the amazon_manager agent) speaks JSON-RPC over stdio
to this script. The script maintains a long-lived streamable-http session
against https://advertising-ai.amazon.com/mcp using an Atza|... bearer that
is automatically refreshed against the LWA refresh_token in
data/vault/credentials.json. Tool / prompt / resource calls are forwarded
verbatim; on a 401 (or any auth-shaped failure) the bearer is refreshed and
the upstream session is reopened transparently.

Access tokens are cached in data/vault/ads_access_token.json (mode 0600) so
short-lived restarts (claude /mcp reconnect, agent supervisor restart) do
not consume a fresh LWA token round-trip every time.

Run modes
---------
    uv run python scripts/amazon_ads_mcp_proxy.py            # stdio MCP server
    uv run python scripts/amazon_ads_mcp_proxy.py --probe    # initialise + list_tools + exit (diagnostic)

No agent code is imported; this script reads the vault JSON directly so it
can be wired into Claude Code without dragging app/ initialisation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import mcp.types as t
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

# --- paths -----------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
VAULT_PATH = REPO_ROOT / "data" / "vault" / "credentials.json"
TOKEN_CACHE_PATH = REPO_ROOT / "data" / "vault" / "ads_access_token.json"

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
UPSTREAM_URL = "https://advertising-ai.amazon.com/mcp"

# Refresh a little before the real expiry so an in-flight call never races it.
EXPIRY_SAFETY_SECS = 120

log = logging.getLogger("amazon_ads_mcp_proxy")


def _read_vault() -> dict[str, str]:
    with VAULT_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_cache() -> dict[str, Any] | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(access_token: str, expires_at: int) -> None:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        TOKEN_CACHE_PATH,
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"access_token": access_token, "expires_at": expires_at}, fh)


def _refresh_lwa_token() -> tuple[str, int]:
    """Mint a new Atza|... access token from the stored refresh_token.

    Returns (access_token, unix_expiry_with_safety_margin).
    """
    vault = _read_vault()
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": vault["ADS_API_REFRESH_TOKEN"],
            "client_id": vault["ADS_API_CLIENT_ID"],
            "client_secret": vault["ADS_API_CLIENT_SECRET"],
        }
    ).encode()
    req = urllib.request.Request(LWA_TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())
    access = payload["access_token"]
    expires_at = int(time.time()) + int(payload["expires_in"]) - EXPIRY_SAFETY_SECS
    _write_cache(access, expires_at)
    log.info("lwa: refreshed access token, valid until unix %d", expires_at)
    return access, expires_at


def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid Atza|... access token, refreshing as needed."""
    if not force_refresh:
        cached = _read_cache()
        if cached and cached.get("expires_at", 0) > int(time.time()):
            return cached["access_token"]
    access, _ = _refresh_lwa_token()
    return access


class UpstreamSession:
    """Holds an open ClientSession against the Amazon Ads MCP endpoint.

    Exposes call() helpers that retry exactly once on auth failure with a
    forced token refresh, so the rest of the proxy can stay oblivious.
    """

    def __init__(self) -> None:
        self._stack = None
        self._session = None
        self._lock = asyncio.Lock()

    async def _open(self) -> None:
        vault = _read_vault()
        access = get_access_token()
        headers = {
            "Authorization": f"Bearer {access}",
            "Amazon-Advertising-API-ClientId": vault["ADS_API_CLIENT_ID"],
        }
        stack = AsyncExitStack()
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamablehttp_client(UPSTREAM_URL, headers=headers)
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        self._stack = stack
        self._session = session
        log.info("upstream: session initialised")

    async def _close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:
                log.warning("upstream: error closing session: %r", exc)
            self._stack = None
            self._session = None

    async def ensure_open(self) -> ClientSession:
        async with self._lock:
            if self._session is None:
                await self._open()
            return self._session

    async def reconnect(self, force_token_refresh: bool) -> ClientSession:
        async with self._lock:
            await self._close()
            if force_token_refresh:
                get_access_token(force_refresh=True)
            await self._open()
            return self._session

    async def call(self, method_name: str, *args, **kwargs):
        """Invoke a ClientSession method, retrying once on auth failure.

        Auth failures from the upstream surface in a few shapes:
          * httpx.HTTPStatusError 401
          * generic exception with '401' / 'unauthor' / 'malformed request uri'
            in its repr (Amazon's non-spec error body)
          * the streamable-http transport closing mid-call
        Heuristic match keeps the proxy resilient without being noisy.
        """
        session = await self.ensure_open()
        try:
            return await getattr(session, method_name)(*args, **kwargs)
        except Exception as exc:
            blob = repr(exc).lower()
            auth_like = (
                "401" in blob
                or "unauthor" in blob
                or "malformed request uri" in blob
                or "invalid_token" in blob
                or "expired" in blob
            )
            if not auth_like:
                raise
            log.warning(
                "upstream: auth-shaped failure on %s — refreshing token and retrying: %r",
                method_name,
                exc,
            )
            session = await self.reconnect(force_token_refresh=True)
            return await getattr(session, method_name)(*args, **kwargs)


def build_server(upstream: UpstreamSession) -> Server:
    server = Server("amazon-ads-proxy")

    @server.list_tools()
    async def _list_tools() -> list[t.Tool]:
        result = await upstream.call("list_tools")
        return list(result.tools)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None):
        result = await upstream.call("call_tool", name, arguments or {})
        return result.content

    @server.list_prompts()
    async def _list_prompts() -> list[t.Prompt]:
        try:
            result = await upstream.call("list_prompts")
        except Exception as exc:
            log.debug("upstream: list_prompts failed (%r) — returning empty", exc)
            return []
        return list(result.prompts)

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, str] | None):
        result = await upstream.call("get_prompt", name, arguments or {})
        return result

    @server.list_resources()
    async def _list_resources() -> list[t.Resource]:
        try:
            result = await upstream.call("list_resources")
        except Exception as exc:
            log.debug("upstream: list_resources failed (%r) — returning empty", exc)
            return []
        return list(result.resources)

    @server.read_resource()
    async def _read_resource(uri):
        result = await upstream.call("read_resource", uri)
        return result.contents

    return server


async def _probe() -> int:
    upstream = UpstreamSession()
    try:
        session = await upstream.ensure_open()
        tools = await session.list_tools()
        names = [tool.name for tool in tools.tools]
        sys.stderr.write(
            f"probe: connected ok; {len(names)} tool(s): {names}\n"
        )
        return 0
    except Exception as exc:
        sys.stderr.write(f"probe: FAILED — {exc!r}\n")
        return 1
    finally:
        await upstream._close()


async def _serve() -> None:
    upstream = UpstreamSession()
    await upstream.ensure_open()
    server = build_server(upstream)
    init_opts = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_opts)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if "--probe" in sys.argv[1:]:
        return asyncio.run(_probe())
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
