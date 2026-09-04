from __future__ import annotations

from collections.abc import Mapping, Set
from datetime import UTC, datetime

from .adapter import IncomingImageAttachment, IncomingMessage


def parse_authorized_message_update(
    update: Mapping[str, object],
    allowed_user_ids: Set[int],
) -> IncomingMessage | None:
    """Return a transport-neutral message for allowlisted private text/photo updates."""

    message = update.get("message")
    if not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    message_id = message.get("message_id")
    date = message.get("date")
    if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
        return None
    if chat.get("type") != "private":
        return None

    chat_id = chat.get("id")
    user_id = sender.get("id")
    if not isinstance(chat_id, int) or not isinstance(user_id, int):
        return None
    if user_id not in allowed_user_ids:
        return None
    if not isinstance(message_id, int):
        return None

    text_value = message.get("text")
    caption_value = message.get("caption")
    text = text_value.strip() if isinstance(text_value, str) else ""
    if not text and isinstance(caption_value, str):
        text = caption_value.strip()

    image = _largest_photo(message.get("photo"))
    images = (image,) if image is not None else ()
    if not text and not images:
        return None

    received_at = (
        datetime.fromtimestamp(date, UTC)
        if isinstance(date, int)
        else datetime.now(UTC)
    )
    return IncomingMessage(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        text=text,
        received_at=received_at,
        images=images,
    )


def parse_authorized_text_update(
    update: Mapping[str, object],
    allowed_user_ids: Set[int],
) -> IncomingMessage | None:
    """Backward-compatible text-only parser retained for legacy call sites/tests."""

    parsed = parse_authorized_message_update(update, allowed_user_ids)
    if parsed is None or parsed.images:
        return None
    return parsed


def _largest_photo(raw: object) -> IncomingImageAttachment | None:
    if not isinstance(raw, list):
        return None
    candidates: list[IncomingImageAttachment] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        file_id = item.get("file_id")
        width = item.get("width")
        height = item.get("height")
        file_size = item.get("file_size")
        if not isinstance(file_id, str) or not file_id.strip():
            continue
        if not isinstance(width, int) or width <= 0:
            continue
        if not isinstance(height, int) or height <= 0:
            continue
        candidates.append(
            IncomingImageAttachment(
                source_id=file_id,
                width=width,
                height=height,
                byte_size_hint=file_size if isinstance(file_size, int) and file_size >= 0 else None,
            )
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.width * item.height,
            item.byte_size_hint if item.byte_size_hint is not None else -1,
        ),
    )
