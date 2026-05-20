"""Abstract transport adapter and global adapter registry.

New messaging platforms implement TransportAdapter and call register_adapter()
at startup. The rest of the system uses the registry to route messages without
knowing which platform is active.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any


@dataclass
class Message:
    """A standardized message object received from any transport."""
    text: str
    timestamp: datetime
    platform: str
    message_id: int | str
    sender_id: str
    display_name: str
    media_items: list[dict] = field(default_factory=list)
    raw_payload: Any = None


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
    async def send_media(
        self, target_id: str | int, data: bytes, mime_type: str, caption: str = ""
    ) -> None:
        """Send a media file (image, audio, video, document) to the given chat/channel.

        Args:
            target_id: The chat/channel to send to.
            data: Raw file bytes.
            mime_type: MIME type (e.g. 'image/png', 'audio/mpeg', 'video/mp4').
            caption: Optional text caption to accompany the media.
        """

    @abstractmethod
    async def download_file(self, file_id: str) -> Optional[tuple[bytes, str, str]]:
        """Download a file attachment. Returns (bytes, mime_type, filename) or None."""

    # ------------------------------------------------------------------
    # Strict variants — used by agent tools that MUST surface the
    # underlying platform error verbatim (Law 6). Existing best-effort
    # ``send_message`` / ``send_media`` methods stay for fire-and-forget
    # poller paths. See docs/TELEGRAM_SKILLS.md for the full contract.
    #
    # Return shape on success::
    #
    #     {"ok": True, "message_id": int, ...}
    #
    # Return shape on failure (NEVER raise NEVER return None)::
    #
    #     {"ok": False, "error_code": int, "description": str}
    #
    # Adapters that do not implement these (e.g. Slack v1, CLI) raise
    # ``NotImplementedError`` from the abstract methods below.
    # ------------------------------------------------------------------

    @abstractmethod
    async def send_text_strict(
        self, target_id: str | int, text: str
    ) -> dict:
        """Strict variant of ``send_message``. See class docstring above."""

    @abstractmethod
    async def send_media_strict(
        self,
        target_id: str | int,
        data: bytes | None,
        mime_type: str,
        caption: str = "",
        *,
        file_id: str | None = None,
        file_type: str | None = None,
        file_ref: str | None = None,
        owner_user_id: str | None = None,
        file_path: str | None = None,
    ) -> dict:
        """Strict variant of ``send_media``.

        Two send modes:
          1. **Re-send by cached file_id** — caller passes ``file_id`` and
             ``file_type``; adapter picks the Bot API method from
             ``file_type`` (sendPhoto / sendDocument / sendAudio /
             sendVideo / sendVoice / sendVideoNote). MIME alone cannot
             disambiguate voice from audio or video_note from video.
          2. **Bytes upload** — caller passes ``data`` + ``mime_type``;
             adapter picks the method via MIME prefix mapping.
             ``file_type`` may be supplied to override the mapping.

        On success, when ``file_ref`` (explicit) or ``file_path``
        (auto-derive) is supplied AND ``owner_user_id`` is non-empty,
        the adapter writes a row to ``outbound_files`` so the file can
        be re-forwarded later without re-uploading bytes. Adapters that
        do not implement a file cache may ignore those kwargs.
        """

    @abstractmethod
    async def copy_message_strict(
        self,
        target_id: str | int,
        from_chat_id: int,
        message_id: int,
    ) -> dict:
        """Strict variant of Telegram's ``copyMessage`` (or platform
        equivalent). Used as the fallback path when ``send_media_strict``
        with a cached ``file_id`` fails."""


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
