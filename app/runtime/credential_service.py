"""ADK 2.0 BaseCredentialService implementation backed by the Ori vault.

Wires `auth_config`-decorated workflow nodes and tools (the OAuth path —
see `app/integrations/`) to the existing vault as the single secret store.
ADK's runtime calls `load_credential` before invoking an authed tool and
`save_credential` after a successful OAuth exchange.

Key derivation: each credential is stored under
    `OAUTH:<scheme_name>:<user_id>`
where scheme_name comes from the AuthConfig and user_id from the callback
context's session state. This namespacing ensures one user's tokens never
collide with another's even when both have authorized the same provider.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.auth.auth_credential import AuthCredential
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.credential_service.base_credential_service import (
    BaseCredentialService,
)

from deploy import vault

logger = logging.getLogger(__name__)


_VAULT_PREFIX = "OAUTH"


def _vault_key(auth_config: AuthConfig, user_id: str) -> str:
    """Namespaced key under which a credential is stored in the vault."""
    scheme_name = _scheme_name(auth_config)
    return f"{_VAULT_PREFIX}:{scheme_name}:{user_id}"


def _scheme_name(auth_config: AuthConfig) -> str:
    """Best-effort name of the auth scheme — used to namespace credentials.

    AuthScheme is a union of FastAPI security scheme types plus ADK custom
    types. Each has a different attribute that identifies it; we try the
    common ones in order and fall back to the type name.
    """
    scheme = auth_config.auth_scheme
    for attr in ("name", "scheme_name", "openIdConnectUrl", "type_"):
        val = getattr(scheme, attr, None)
        if isinstance(val, str) and val:
            return val.replace(":", "_")
    return type(scheme).__name__


def _user_id_from(callback_context: CallbackContext) -> str:
    """Extract the speaker's user id from session state. Falls back to a
    `_global` namespace when no user is bound (e.g. service-only tasks).
    """
    state_dict = callback_context.state.to_dict() if callback_context.state else {}
    return state_dict.get("user_id") or "_global"


class OriCredentialService(BaseCredentialService):
    """Vault-backed credential service for OAuth tokens and similar secrets."""

    async def load_credential(
        self,
        auth_config: AuthConfig,
        callback_context: CallbackContext,
    ) -> Optional[AuthCredential]:
        user_id = _user_id_from(callback_context)
        key = _vault_key(auth_config, user_id)
        raw = vault.get(key, "")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            logger.warning("OriCredentialService: corrupted vault entry %s: %s", key, e)
            return None
        try:
            return AuthCredential.model_validate(data)
        except Exception as e:
            logger.warning(
                "OriCredentialService: failed to validate AuthCredential at %s: %s", key, e
            )
            return None

    async def save_credential(
        self,
        auth_config: AuthConfig,
        callback_context: CallbackContext,
    ) -> None:
        # Convention from the ADK base class: the credential being saved is on
        # auth_config.exchanged_auth_credential (post-OAuth-exchange) or
        # raw_auth_credential. We persist whichever is non-None, preferring
        # exchanged.
        cred = (
            auth_config.exchanged_auth_credential
            or auth_config.raw_auth_credential
        )
        if cred is None:
            logger.warning("OriCredentialService.save_credential called with no credential")
            return
        user_id = _user_id_from(callback_context)
        key = _vault_key(auth_config, user_id)
        vault.set(key, cred.model_dump_json(exclude_none=True))
        logger.info("OriCredentialService: saved credential under %s", key)
