"""Amazon Ads API — 3-legged OAuth helper.

Mints an ADS_API_REFRESH_TOKEN against the Ads LWA app whose client_id /
client_secret live in the vault (data/vault/credentials.json), then writes
the resulting refresh_token back into the same vault.

Usage
-----
Auto mode (browser on the same machine as the helper):

    uv run python scripts/ads_oauth_helper.py

Manual mode (browser on a different machine — e.g. the seller-account
owner is in another country and you need them to perform the consent
grant from their own browser, then paste the redirected URL back to you):

    uv run python scripts/ads_oauth_helper.py --manual

Prerequisites
-------------
1. ADS_API_CLIENT_ID and ADS_API_CLIENT_SECRET already in the vault.
2. The Ads LWA Security Profile at https://developer.amazon.com/loginwithamazon/
   has  http://localhost:8765/callback  added under Web Settings →
   "Allowed Return URLs".
3. The person running the consent grant must be signed into the Amazon
   account that owns the Seller Central seller(s) you want the agent to
   read ads data for.

Auto-mode flow
--------------
1. Start a one-shot localhost HTTP server on 127.0.0.1:8765.
2. Build the authorize URL with scope `advertising::campaign_management`
   and the registered redirect_uri.
3. Open it in the default browser (or print it if headless).
4. Amazon shows the consent screen; on grant it redirects back to the
   local server with `?code=...`.
5. Server captures the code, POSTs it to https://api.amazon.com/auth/o2/token
   with grant_type=authorization_code, receives refresh_token + access_token.
6. refresh_token is written to vault via vault.set("ADS_API_REFRESH_TOKEN", ...).
7. Server shuts down, script exits.

Manual-mode flow
----------------
1. Skip the local server entirely.
2. Print the authorize URL — send it to the remote operator.
3. Remote operator opens the URL in their own browser, signs in, grants
   consent. Amazon redirects them to http://localhost:8765/callback?code=...
   on their machine; they see a connection-refused error page, but the
   full URL (including the code + state in the query string) is visible
   in their browser's address bar.
4. Remote operator copies the entire URL bar contents and sends it back.
5. Paste that URL at the prompt; helper extracts the code, validates the
   state token, swaps for tokens, writes refresh_token to vault.

Re-running rotates the refresh_token (Amazon issues a new one on every
consent grant). Don't run if you don't want rotation.
"""

import argparse
import http.server
import logging
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

import httpx

# Hydrate vault-backed secrets into os.environ before anything else.
from deploy.vault import load_vault, set as vault_set

load_vault()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ads_oauth")

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
AUTHORIZE_URL = "https://www.amazon.com/ap/oa"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
SCOPE = "advertising::campaign_management"

# Public-mode constants — used when the consent grant is performed by a
# remote account owner against the bot's public ``/oauth/ads/callback``
# endpoint instead of a local HTTP server. Must stay in sync with
# ``app/a2a_server.py:_OAUTH_ADS_CALLBACK_PATH`` and ``_ads_oauth_state_is_valid``.
_PUBLIC_BASE_URL = "https://bezosapp.uk"
_PUBLIC_CALLBACK_PATH = "/oauth/ads/callback"
_PUBLIC_REDIRECT_URI = f"{_PUBLIC_BASE_URL}{_PUBLIC_CALLBACK_PATH}"
_PUBLIC_STATE_HMAC_LEN = 32

# Filled in by the callback handler.
_received: dict[str, str | None] = {"code": None, "state": None, "error": None}
_expected_state: str | None = None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib API)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found.")
            return

        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]
        error_desc = params.get("error_description", [None])[0]

        if error:
            _received["error"] = f"{error}: {error_desc}"
            body = f"<h1>OAuth error</h1><p>{error}</p><p>{error_desc}</p>".encode()
            self.send_response(400)
        elif not code:
            _received["error"] = "no_code"
            body = b"<h1>OAuth error</h1><p>No authorization code returned.</p>"
            self.send_response(400)
        elif state != _expected_state:
            _received["error"] = f"state_mismatch: got {state!r}, expected {_expected_state!r}"
            body = b"<h1>OAuth error</h1><p>CSRF state mismatch.</p>"
            self.send_response(400)
        else:
            _received["code"] = code
            _received["state"] = state
            body = (
                b"<h1>Authorization captured.</h1>"
                b"<p>You can close this tab. The helper script will continue.</p>"
            )
            self.send_response(200)

        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002, ARG002 (silence noisy stdlib logs)
        return


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _exchange_code_for_tokens(code: str, client_id: str, client_secret: str) -> dict:
    """POST the authorization code to LWA, return the parsed token response."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    resp = httpx.post(TOKEN_URL, data=data, timeout=30.0)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Token exchange failed: HTTP {resp.status_code} — {resp.text}"
        )
    payload = resp.json()
    if "refresh_token" not in payload:
        raise RuntimeError(f"Token endpoint returned no refresh_token: {payload}")
    return payload


def _build_authorize_url(client_id: str, state: str) -> str:
    qs = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": SCOPE,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{qs}"


def _parse_callback_url(url: str, expected_state: str) -> str:
    """Pull the ``code`` query param out of a redirected callback URL.

    Accepts the full URL the operator copy-pasted from the address bar
    of their browser (the page they saw was a connection-refused error,
    but the URL still carries the OAuth grant). Validates the state
    parameter to defeat replay / CSRF attempts.
    """
    parsed = urllib.parse.urlparse(url.strip())
    params = urllib.parse.parse_qs(parsed.query)
    if "error" in params:
        raise RuntimeError(
            f"Amazon returned an OAuth error: "
            f"{params.get('error', ['unknown'])[0]} — "
            f"{params.get('error_description', [''])[0]}"
        )
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    if not code:
        raise RuntimeError(
            "URL has no `code` query parameter. Make sure you pasted the FULL "
            "URL from the address bar after Amazon redirected (the page itself "
            "will show a 'site can't be reached' error — that's expected)."
        )
    if state != expected_state:
        raise RuntimeError(
            f"CSRF state mismatch — got {state!r}, expected {expected_state!r}. "
            "Re-run the helper to issue a fresh state and authorize URL."
        )
    return code


def _run_auto_mode(client_id: str, client_secret: str) -> int:
    """Browser on this machine — start a one-shot localhost callback server."""
    if not _port_is_free(REDIRECT_HOST, REDIRECT_PORT):
        print(
            f"ERROR: port {REDIRECT_PORT} is already in use. Free it and re-run.",
            file=sys.stderr,
        )
        return 2

    global _expected_state
    _expected_state = secrets.token_urlsafe(24)
    authorize_url = _build_authorize_url(client_id, _expected_state)

    server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    logger.info("Listening on %s for the OAuth callback.", REDIRECT_URI)
    logger.info("If your browser does not open automatically, paste this URL:")
    print()
    print(authorize_url)
    print()
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass

    logger.info("Waiting for Amazon to redirect back (Ctrl-C to abort)...")
    try:
        while _received["code"] is None and _received["error"] is None:
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        logger.warning("Aborted by user.")
        server.shutdown()
        return 130

    server.shutdown()

    if _received["error"]:
        print(f"\nOAuth error: {_received['error']}", file=sys.stderr)
        return 1

    code = _received["code"]
    assert code is not None
    return _exchange_and_store(code, client_id, client_secret)


def _public_state(secret: bytes) -> str:
    """Issue an HMAC-signed state token the bot's ``/oauth/ads/callback``
    will accept. Format: ``<nonce_hex>.<mac_hex_truncated>``."""
    import hashlib
    import hmac

    nonce = secrets.token_hex(16)
    mac = hmac.new(secret, nonce.encode(), hashlib.sha256).hexdigest()[
        :_PUBLIC_STATE_HMAC_LEN
    ]
    return f"{nonce}.{mac}"


def _run_public_mode(client_id: str, _client_secret_unused: str) -> int:
    """Print an authorize URL pointing at the bot's public OAuth callback.

    No local server, no waiting. The account owner clicks the URL,
    Amazon redirects them to ``<A2A_BASE_URL>/oauth/ads/callback`` on
    our public tunnel, the bot's handler swaps the code for a refresh
    token and writes it to the vault on the host where the bot runs.

    The state token is HMAC-signed with ``A2A_API_KEY`` so the bot can
    verify the redirect came from a link we issued without sharing any
    cross-process state.
    """
    a2a_key = os.environ.get("A2A_API_KEY", "").strip()
    if not a2a_key:
        print(
            "ERROR: A2A_API_KEY not in the vault. The callback handler uses "
            "it to verify the OAuth state, so the helper must use the same "
            "key to sign. Populate it and re-run.",
            file=sys.stderr,
        )
        return 2

    state = _public_state(a2a_key.encode())
    qs = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": SCOPE,
            "response_type": "code",
            "redirect_uri": _PUBLIC_REDIRECT_URI,
            "state": state,
        }
    )
    authorize_url = f"{AUTHORIZE_URL}?{qs}"

    print()
    print(f"Authorize URL: {authorize_url}")
    print(f"Redirect URI:  {_PUBLIC_REDIRECT_URI}")
    print(f"A2A_BASE_URL env: {os.environ.get('A2A_BASE_URL', '<unset, default https://bezosapp.uk>')!r}")
    print()
    print(
        "Send the authorize URL to the account owner. Once they click it "
        "and approve, the bot writes ADS_API_REFRESH_TOKEN to its vault."
    )
    return 0


def _run_manual_mode(client_id: str, client_secret: str) -> int:
    """Browser on another machine — print the authorize URL, wait for the
    operator on the other end to paste the redirected URL back."""
    state = secrets.token_urlsafe(24)
    authorize_url = _build_authorize_url(client_id, state)

    print()
    print("=" * 78)
    print("MANUAL OAUTH — send the authorize URL to the account owner")
    print("=" * 78)
    print()
    print("Step 1. Send this URL to the Amazon account owner:")
    print()
    print(authorize_url)
    print()
    print("Step 2. Owner opens it in their browser, signs in to the Amazon")
    print("        account that owns the Ads / seller account, clicks 'Allow'.")
    print()
    print("Step 3. Amazon redirects to http://localhost:8765/callback?code=...")
    print("        The page shows a connection-refused error — that is fine.")
    print("        The owner copies the FULL URL from the address bar and")
    print("        sends it back to you.")
    print()
    print("Step 4. Paste that URL here:")
    print()

    try:
        pasted = input("URL: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 130

    if not pasted:
        print("ERROR: no URL provided.", file=sys.stderr)
        return 1

    try:
        code = _parse_callback_url(pasted, state)
    except Exception as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    return _exchange_and_store(code, client_id, client_secret)


def _exchange_and_store(code: str, client_id: str, client_secret: str) -> int:
    logger.info("Authorization code captured. Exchanging for tokens...")
    try:
        tokens = _exchange_code_for_tokens(code, client_id, client_secret)
    except Exception as exc:
        print(f"\nToken exchange failed: {exc}", file=sys.stderr)
        return 1

    refresh_token = tokens["refresh_token"]
    vault_set("ADS_API_REFRESH_TOKEN", refresh_token)
    logger.info("ADS_API_REFRESH_TOKEN written to vault.")
    print()
    print("Done. Refresh token saved.")
    print("Restart the bot so vault.load_vault() picks it up.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint an Amazon Ads API refresh token via 3-legged OAuth."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--manual",
        action="store_true",
        help=(
            "Manual mode — print the authorize URL for an out-of-band browser "
            "session, then wait for the operator on the other end to paste "
            "the redirected URL back. Used when the remote browser cannot "
            "reach the bot's public endpoint. Redirect URI is "
            "http://localhost:8765/callback."
        ),
    )
    mode.add_argument(
        "--public",
        action="store_true",
        help=(
            "Public mode — print an authorize URL whose redirect_uri points "
            "at the bot's public /oauth/ads/callback endpoint "
            "(default: $A2A_BASE_URL → https://bezosapp.uk). The bot captures "
            "the code, swaps it for a refresh token, and writes it to its "
            "own vault. The account owner sees a success page and is done — "
            "nothing to copy back. Recommended for non-technical owners."
        ),
    )
    args = parser.parse_args()

    client_id = os.environ.get("ADS_API_CLIENT_ID", "").strip()
    client_secret = os.environ.get("ADS_API_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "ERROR: ADS_API_CLIENT_ID / ADS_API_CLIENT_SECRET not in the vault.\n"
            "       Edit data/vault/credentials.json (or use the /init flow) "
            "to add them, then re-run.",
            file=sys.stderr,
        )
        return 2

    if args.public:
        return _run_public_mode(client_id, client_secret)
    if args.manual:
        return _run_manual_mode(client_id, client_secret)
    return _run_auto_mode(client_id, client_secret)


if __name__ == "__main__":
    sys.exit(main())
