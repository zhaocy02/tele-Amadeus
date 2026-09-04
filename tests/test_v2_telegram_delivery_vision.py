import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from amadeus_bot.telegram import (
    DownloadedImage,
    IncomingImageAttachment,
    IncomingMessage,
    V2TelegramDeliveryAdapter,
)


@dataclass
class FakePrepared:
    reply_text: str = "v2 reply"


@dataclass
class FakeFinalize:
    exchange: object | None = object()
    warnings: tuple[str, ...] = ()


class FakeConversation:
    def __init__(self) -> None:
        self.text: str | None = None
        self.user_images: tuple[object, ...] = ()
        self.finalized = False

    async def prepare_user_turn(self, chat_id: int, text: str, **kwargs: object) -> FakePrepared:
        self.text = text
        images = kwargs.get("user_images")
        assert isinstance(images, tuple)
        self.user_images = images
        return FakePrepared()

    async def finalize_delivered_turn(self, prepared: object, **kwargs: object) -> FakeFinalize:
        self.finalized = True
        return FakeFinalize()


class FakeGateway:
    def __init__(self) -> None:
        self.downloaded: list[str] = []
        self.sent: list[str] = []

    async def download_image(self, attachment: IncomingImageAttachment, *, max_bytes: int):
        assert max_bytes == 8 * 1024 * 1024
        self.downloaded.append(attachment.source_id)
        return DownloadedImage(data=b"jpeg-bytes", media_type="image/jpeg")

    async def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append(text)
        return 500

    async def send_typing(self, chat_id: int) -> None:
        return None


class UnusedAutonomy:
    pass


class UnusedAutonomyGenerator:
    pass


class UnusedSessions:
    pass


def _adapter() -> tuple[V2TelegramDeliveryAdapter, FakeGateway, FakeConversation]:
    gateway = FakeGateway()
    conversation = FakeConversation()
    adapter = V2TelegramDeliveryAdapter(
        gateway=gateway,  # type: ignore[arg-type]
        conversation=conversation,  # type: ignore[arg-type]
        autonomy=UnusedAutonomy(),  # type: ignore[arg-type]
        autonomy_message_generator=UnusedAutonomyGenerator(),  # type: ignore[arg-type]
        sessions=UnusedSessions(),  # type: ignore[arg-type]
    )
    return adapter, gateway, conversation


def _photo(caption: str = "") -> IncomingMessage:
    return IncomingMessage(
        chat_id=42,
        user_id=42,
        message_id=7,
        text=caption,
        received_at=datetime.now(UTC),
        images=(
            IncomingImageAttachment(
                source_id="opaque-telegram-file-id",
                width=1280,
                height=720,
                byte_size_hint=4096,
            ),
        ),
    )


def test_photo_caption_becomes_bounded_transcript_marker_and_image_is_ephemeral() -> None:
    async def scenario() -> None:
        adapter, gateway, conversation = _adapter()
        result = await adapter.deliver_user_message(_photo("你看看这个"))

        assert result.telegram_message_id == 500
        assert gateway.downloaded == ["opaque-telegram-file-id"]
        assert conversation.text == "[User sent a photo]\nCaption: 你看看这个"
        assert len(conversation.user_images) == 1
        image = conversation.user_images[0]
        assert image.data == b"jpeg-bytes"  # type: ignore[attr-defined]
        assert conversation.finalized is True
        assert gateway.sent == ["v2 reply"]

    asyncio.run(scenario())


def test_photo_without_caption_still_generates_a_turn() -> None:
    async def scenario() -> None:
        adapter, _, conversation = _adapter()
        await adapter.deliver_user_message(_photo())
        assert conversation.text == "[User sent a photo]"

    asyncio.run(scenario())
