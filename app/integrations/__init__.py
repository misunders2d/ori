"""OAuth integrations subsystem.

Drop a new provider by:
1. Adding `app/integrations/<name>.py` with a `IntegrationProvider` subclass.
2. Importing it here so the module-level `register_provider(<name>())` runs.
3. Setting vault keys `<NAME>_OAUTH_CLIENT_ID` / `_CLIENT_SECRET`.

The REGISTRY is the single lookup point. Tools and the OAuth callback
handler use `REGISTRY[name]` to dispatch flows.
"""

from __future__ import annotations

from app.integrations.base import IntegrationProvider
from app.integrations.clickup import ClickUpProvider
from app.integrations.github import GitHubProvider
from app.integrations.google import GoogleProvider
from app.integrations.slack import SlackProvider

REGISTRY: dict[str, IntegrationProvider] = {}


def register_provider(provider: IntegrationProvider) -> None:
    """Register an instance; later registrations replace earlier ones."""
    REGISTRY[provider.name] = provider


def get_provider(name: str) -> IntegrationProvider:
    """Look up a provider; raises KeyError on unknown name."""
    return REGISTRY[name]


def list_providers() -> list[str]:
    """Provider names available right now."""
    return sorted(REGISTRY.keys())


# Self-register the bundled providers on package import.
register_provider(GoogleProvider())
register_provider(GitHubProvider())
register_provider(ClickUpProvider())
register_provider(SlackProvider())


__all__ = [
    "REGISTRY",
    "ClickUpProvider",
    "GitHubProvider",
    "GoogleProvider",
    "IntegrationProvider",
    "SlackProvider",
    "get_provider",
    "list_providers",
    "register_provider",
]
