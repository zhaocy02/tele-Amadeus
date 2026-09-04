import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from amadeus_bot.telegram import (
    IncomingImageAttachment,
    IncomingMessage,
    TelegramHTTPGateway,
    TelegramInboxStore,
    TelegramTransportError,
    parse_authorized_message_update,
)


def _photo_update(*, user_id: int = 10, caption: str | None = None) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": 9,
        "date": 1_700_000_000,
        "chat": {"id": 10, "type": "private"},
        "from": {"id": user_id},
        "photo": [
            {"file_id": "small", "width": 90, "height": 90, "file_size": 1000},
            {"file_id": "large", "width": 1280, "height": 720, "file_size": 4000},
        ],
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": 77, "message": message}


def test_authorized_photo_parser_selects_largest_size_and_caption() -> None:
    message = parse_authorized_message_update(_photo_update(caption="  看这个  "), {10})
    assert message is not None
    assert message.text == "看这个"
    assert len(message.images) == 1
    assert message.images[0].source_id == "large"
    assert message.images[0].width == 1280
    assert message.images[0].byte_size_hint == 4000


def test_unauthorized_photo_is_rejected_before_media_resolution() -> None:
    assert parse_authorized_message_update(_photo_update(user_id=99), {10}) is None


def test_inbox_round_trips_photo_reference_without_binary(tmp_path: Path) -> None:
    inbox = TelegramInboxStore(tmp_path / "inbox.sqlite")
    message = IncomingMessage(
        chat_id=10,
        user_id=10,
        message_id=9,
        text="",
        received_at=datetime.now(UTC),
        images=(
            IncomingImageAttachment(
                source_id="opaque-file-id",
                width=640,
                height=480,
                byte_size_hint=2048,
            ),
        ),
    )
    try:
        assert inbox.enqueue(77, message) is True
        pending = inbox.pending()
        assert len(pending) == 1
        restored = pending[0].message
        assert restored.text == ""
        assert restored.images == message.images
    finally:
        inbox.close()


def test_gateway_getfile_and_download_are_bounded_and_sanitized() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/getFile"):
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {"file_path": "photos/test.jpg", "file_size": 4},
                    },
                )
            if request.method == "GET" and request.url.path.endswith("/photos/test.jpg"):
                return httpx.Response(200, content=b"jpeg")
            return httpx.Response(404)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gateway = TelegramHTTPGateway(bot_token="SECRET", client=client)
        image = await gateway.download_image(
            IncomingImageAttachment(source_id="file", width=100, height=100, byte_size_hint=4),
            max_bytes=8,
        )
        assert image.data == b"jpeg"
        assert image.media_type == "image/jpeg"

        with pytest.raises(TelegramTransportError, match="byte limit") as captured:
            await gateway.download_image(
                IncomingImageAttachment(
                    source_id="file",
                    width=100,
                    height=100,
                    byte_size_hint=9,
                ),
                max_bytes=8,
            )
        assert "SECRET" not in str(captured.value)
        await client.aclose()

    asyncio.run(scenario())
