# Headless Integration Patterns (The "Dark Server" Strategy)

When building integrations for platforms that require OAuth2 (Google Drive, Meet, Facebook, etc.) on a remote server with no browser and no open inbound ports, use these pre-approved patterns.

## 1. Out-of-Band (OOB) / Copy-Paste Flow (Preferred)
Use this when you want to keep the server 100% "silent" (all inbound ports blocked).

*   **Workflow:**
    1.  Generate an Authorization URL using the platform's "Desktop" or "OOB" redirect URI (e.g., `urn:ietf:wg:oauth:2.0:oob` for Google).
    2.  Send the link to the user via the messaging adapter (Telegram).
    3.  The user opens the link on their own device, authorizes the app, and copies the resulting "Authorization Code."
    4.  The user pastes the code back into the chat.
    5.  The agent intercepts the message via the "Secure Key Capture" mechanism, exchanges the code for a token, and deletes the user's message.
*   **Best for:** Most standard OAuth2 integrations.

## 2. Device Code Flow (Google/GitHub/Microsoft)
Excellent for platforms that explicitly support "Device Auth."

*   **Workflow:**
    1.  Request a `device_code` and `user_code` from the platform's auth endpoint.
    2.  Send the `verification_url` and the `user_code` to the user.
    3.  The user visits the URL on their own device and enters the code.
    4.  The agent's background task polls the platform's token endpoint until the user finishes (or the code expires).
*   **Best for:** Google Drive, Meet, GitHub, and other modern APIs.

## 3. Ephemeral Tunnel (Last Resort)
Use this only if a platform *strictly* requires a valid `https` redirect URL and does not support OOB or Device flows.

*   **Workflow:**
    1.  Programmatically spin up a transient tunnel (e.g., Cloudflare Tunnel or localtunnel).
    2.  Launch a temporary web server on a local port.
    3.  Generate the Auth URL pointing to the temporary tunnel address.
    4.  Once the token is received at the endpoint, **immediately kill both the web server and the tunnel process.**
*   **Security Constraint:** The tunnel must never be persistent. It only exists for the duration of the handshake.

## Implementation Guide
When a user asks to "connect to [Platform]", the agent should:
1.  Check the platform's documentation for "Device Flow" support.
2.  If not available, default to the "OOB/Copy-Paste" strategy.
3.  Draft a tool in `app/tools/` that implements the chosen flow.
4.  Utilize `app/secure_config.py` to prompt the human for the required credentials/codes securely.
