"""Diagnostic: probe each stored Google refresh token and print Google's verdict.

Run from the repo root:

    uv run python scripts/check_google_refresh.py

Hydrates GOOGLE_OAUTH_CLIENT_ID / _SECRET from the project's vault
(data/vault/credentials.json) via deploy.vault.load_vault — the same path
the bot itself uses at startup, so no .env is required.

For each row in data/oauth_tokens.db, this attempts an offline refresh against
Google's token endpoint (same code path the agent uses) and prints the raw
response. If Google returns an error, the `message` field carries its body —
`invalid_grant` means the refresh token is dead (rotated, revoked, or
expired), `invalid_client` means the app's client_id/secret is wrong, etc.
"""

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

# Hydrate vault-backed secrets into os.environ before anything else imports settings.
from deploy.vault import load_vault

load_vault()

from app.tools.google_oauth.web_flow import refresh_access_token  # noqa: E402


DB_PATH = Path("data/oauth_tokens.db")


async def main() -> None:
    if not DB_PATH.exists():
        print(f"No DB at {DB_PATH.resolve()} — nothing to check.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT email, refresh_token, access_token, expiry FROM tokens"
        ).fetchall()

    print(f"Found {len(rows)} token row(s) in {DB_PATH}.\n")

    for email, refresh_token, access_token, expiry in rows:
        when = datetime.fromtimestamp(expiry).isoformat(timespec="seconds")
        print(f"--- {email} ---")
        print(f"  stored access_token length : {len(access_token)}")
        print(f"  stored refresh_token length: {len(refresh_token)}")
        print(f"  access_token expiry        : {when}")

        try:
            result = await refresh_access_token(refresh_token)
        except Exception as exc:
            print(f"  refresh EXCEPTION          : {type(exc).__name__}: {exc}\n")
            continue

        status = result.get("status")
        if status == "success":
            new_len = len(result.get("access_token", ""))
            exp_in = result.get("expires_in")
            print(f"  refresh status             : success")
            print(f"  new access_token length    : {new_len}")
            print(f"  new token expires_in (s)   : {exp_in}")
        else:
            print(f"  refresh status             : {status}")
            print(f"  google message             : {result.get('message')}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
