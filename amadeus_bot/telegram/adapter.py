from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IncomingImageAttachment:
    """Opaque remote image reference staged before bounded download."""

    source_id: str
    width: int
    height: int
    byte_size_hint: int | None = None
    media_type: str = "image/jpeg"

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("image source_id must not be empty")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")
        if self.byte_size_hint is not None and self.byte_size_hint < 0:
            raise ValueError("image byte_size_hint must not be negative")


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    """Ephemeral downloaded image payload; never intended for persistence."""

    data: bytes
    media_type: str

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("downloaded image must not be empty")
        if not self.media_type.startswith("image/"):
            raise ValueError("downloaded media_type must be an image type")


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """Transport-neutral representation of an authorized Telegram text/photo message."""

    chat_id: int
    user_id: int
    message_id: int
    text: str
    received_at: datetime
    images: tuple[IncomingImageAttachment, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip() and not self.images:
            raise ValueError("incoming message must contain text or an image")


class TelegramGateway(Protocol):
    """Minimal Telegram surface used by the character runtime."""

    async def send_message(self, chat_id: int, text: str) -> int:
        """Send text and return the resulting Telegram message ID."""
        ...

    async def send_typing(self, chat_id: int) -> None:
        """Emit a transient typing action."""
        ...

    async def download_image(
        self,
        attachment: IncomingImageAttachment,
        *,
        max_bytes: int,
    ) -> DownloadedImage:
        """Resolve and download one authorized remote image with a hard byte limit."""
        ...
