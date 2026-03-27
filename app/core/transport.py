"""Abstract transport adapter and global adapter registry.

New messaging platforms implement TransportAdapter and call register_adapter()
at startup. The rest of the system uses the registry to route messages without
knowing which platform is active.
"""

from abc import ABC, abstractmethod
from typing import Optional


class TransportAdapter(ABC):
    """Base class for all messaging platform adapters."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Short identifier, e.g. 'telegram', 'slack', 'discord'."""

    @abstractmethod
    def make_session_id(self, raw_id: str | int) -> str:
        """Build a canonical session ID from a platform-specific chat/channel ID."""

    @abstractmethod
    def make_user_id(self, raw_id: str | int) -> str:
        """Build a canonical user ID from a platform-specific user ID."""

    @abstractmethod
    def parse_notify_info(self, session_id: str) -> dict:
        """Extract notification routing info from a session ID.

        Returns e.g. {"type": "telegram", "chat_id": 123} or {} if
        this adapter does not own the given session_id.
        """

    @abstractmethod
    async def send_message(self, target_id: str | int, text: str) -> None:
        """Send a text message to the given chat/channel."""

    @abstractmethod
    async def send_typing(self, target_id: str | int) -> None:
        """Send a typing indicator."""

    @abstractmethod
    async def delete_message(self, target_id: str | int, message_id: int) -> None:
        """Delete a specific message (best-effort)."""

    @abstractmethod
    async def download_file(self, file_id: str) -> Optional[tuple[bytes, str, str]]:
        """Download a file attachment. Returns (bytes, mime_type, filename) or None."""


# ---------------------------------------------------------------------------
# Global adapter registry
# ---------------------------------------------------------------------------

_registry: dict[str, TransportAdapter] = {}


def register_adapter(adapter: TransportAdapter):
    """Register a transport adapter by its platform name."""
    _registry[adapter.platform_name] = adapter


def get_adapter(name: str) -> TransportAdapter | None:
    """Retrieve a registered adapter by platform name."""
    return _registry.get(name)


def get_all_adapters() -> dict[str, TransportAdapter]:
    """Return a copy of the full adapter registry."""
    return dict(_registry)


def parse_notify_from_session_id(session_id: str) -> dict:
    """Ask all registered adapters to parse notification info from a session ID.

    Returns the first non-empty result, or {} if no adapter claims the ID.
    """
    for adapter in _registry.values():
        info = adapter.parse_notify_info(session_id)
        if info:
            return info
    return {}
