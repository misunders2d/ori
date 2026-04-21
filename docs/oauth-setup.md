# Google OAuth Setup — Operational Runbook

How the Google OAuth integration is wired up end-to-end. Written after the
device-flow → web-flow migration (commit `bdc0d54`, 2026-04-21). Use this as
the reference when anything breaks or needs changing.

## Why web flow, not device flow

Google's limited-input device flow explicitly does not support Gmail scopes
(allowlist: `email/openid/profile`, `drive.appdata/drive.file`, `youtube/youtube.readonly`
only). Any attempt to include `gmail.readonly` returns `Invalid device flow scope`.
The web flow (Authorization Code + PKCE) supports all scopes.

## Infra components

| Layer | What | Where |
|-------|------|-------|
| Public domain | `bezosapp.uk` | Cloudflare Registrar (account: `2djohar@gmail.com`) |
| Public HTTPS endpoint | `https://bezosapp.uk` | Cloudflare Zero Trust named tunnel |
| Tunnel name | `bezos-bot` | Cloudflare Zero Trust → Networks → Connectors |
| Tunnel target | `localhost:8002` (A2A_PORT) | VPS systemd service `cloudflared` |
| OAuth client (active) | "bezos web" (Web application) | GCP project `Mellanni-Project-DA` → Credentials |
| OAuth client (legacy) | "bezos connector" (TV and Limited Input) | Same project, unused — kept as rollback |
| Callback route | `/oauth/google/callback` | `app/a2a_server.py` middleware |
| Token store | SQLite at `data/oauth_tokens.db` | Per-user, keyed by email |

## Vault keys required

Set via `/init <PASSCODE> KEY=VALUE` in Slack (allowlist in `app/app_utils/config.py:10`):

- `GOOGLE_OAUTH_CLIENT_ID` — from the "bezos web" OAuth client
- `GOOGLE_OAUTH_CLIENT_SECRET` — from the same client
- `OAUTH_BASE_URL` — `https://bezosapp.uk`

## GCP OAuth consent screen

- Publishing status: **Testing** (NOT Production).
- User type: **External**.
- Restricted scopes: `gmail.readonly` is the only restricted one. Drive/Sheets/Calendar are sensitive but not restricted.
- **Test users** is the key list. Only emails on this list can consent while in Testing mode. Managed at GCP → Google Auth Platform → Audience → Test users.

## Adding a new user (non-test)

For any email not already on the Test users list:

1. GCP Console → APIs & Services → OAuth consent screen → Audience.
2. Test users → **+ Add users** → paste the email → Save.
3. Tell the user to run `google_connect` in Slack/Telegram, click the auth URL.
4. On the "This app isn't verified" warning screen, they click **Advanced** → **Go to bezos web (unsafe)** → proceed. This warning is cosmetic and unavoidable in Testing mode for restricted scopes.
5. After consent, the callback persists tokens. They can now use any Google tool.

100-user limit while in Testing. That's plenty for personal/small-team use.

## Going fully public (verification)

Not done, probably not worth it. Google requires CASA security audit ($15k–$75k) and weeks of review for apps using restricted scopes. Only pursue if shipping as a real external product.

## Changing the public URL

If you move off `bezosapp.uk`:

1. Register the new domain in Cloudflare.
2. Update the tunnel's public hostname routing in Cloudflare Zero Trust.
3. In GCP → Credentials → bezos web → Authorized redirect URIs: add the new `https://<new-domain>/oauth/google/callback`. Keep the old one until no users need it.
4. Update vault: `/init <PASSCODE> OAUTH_BASE_URL=https://<new-domain>`.
5. Restart the bot.
6. All currently-connected users keep working (their tokens are still valid); only new connects use the new URL.

## Changing A2A_PORT

If `A2A_PORT` in the vault changes (e.g., after redeploy or port conflict resolution):

1. Cloudflare Zero Trust → tunnel `bezos-bot` → Public Hostname → edit the route → update Service URL from `localhost:<old>` to `localhost:<new>`.

The OAuth flow has no other coupling to the port — it reads `OAUTH_BASE_URL` (hostname only) from the vault.

## Debugging

- **"Invalid device flow scope" error** → you're still on the old code path. Pull master.
- **"Access blocked" on Google screen** → user isn't in Test users list. Add them.
- **"redirect_uri_mismatch" error** → the redirect URI in the GCP client doesn't match `OAUTH_BASE_URL + /oauth/google/callback`. Check both.
- **Callback returns 502 / Cloudflare error** → tunnel routing is broken. Verify `curl -I https://bezosapp.uk` returns the A2A 401 JSON.
- **"Invalid or unknown state parameter"** → the pending-state dict was cleared (bot restart between `google_connect` and the callback). Tell the user to run `google_connect` again.
- **Refresh fails on existing tokens after client migration** → refresh tokens are tied to the OAuth client ID that issued them. If you rotate OAuth clients, all users must reconnect.

## Deleting the legacy "bezos connector"

Keep it for now (costs nothing). Delete once you're confident no rollback is needed — probably after one successful week of web-flow operation with zero reconnect issues.

## Related code

- `app/tools/google_oauth/web_flow.py` — PKCE flow implementation
- `app/a2a_server.py` — callback route in the Starlette middleware
- `app/tools/google_drive.py:google_connect` — tool entry point
- `app/tools/google_oauth/token_store.py` — per-user token persistence
- `app/app_utils/config.py:10` — vault key allowlist
