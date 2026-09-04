import asyncio
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.telegram import IncomingMessage, TelegramInboxStore, V2TelegramPollingRunner


class FakePollingGateway:
    def __init__(self) -> None:
        self.calls: list[int | None] = []
        self.sent = False

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 25,
    ):  # type: ignore[no-untyped-def]
        self.calls.append(offset)
        await asyncio.sleep(0)
        if not self.sent:
            self.sent = True
            return (
                {
                    "update_id": 77,
                    "message": {
                        "message_id": 9,
                        "date": 1_700_000_000,
                        "text": "hello v2",
                        "chat": {"id": 10, "type": "private"},
                        "from": {"id": 10},
                    },
                },
            )
        await asyncio.sleep(0.01)
        return ()


class RecordingHandler:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []

    async def handle(self, message: IncomingMessage) -> None:
        self.messages.append(message)
        await asyncio.sleep(0)


def test_polling_stages_authorized_update_and_processes_it_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway = FakePollingGateway()
        handler = RecordingHandler()
        inbox = TelegramInboxStore(tmp_path / "inbox.sqlite")
        runner = V2TelegramPollingRunner(
            gateway=gateway,
            handler=handler,
            inbox=inbox,
            allowed_user_ids={10},
            poll_timeout_seconds=1,
            retry_delay_seconds=0,
        )
        stop = asyncio.Event()
        try:
            completed = await runner.run(
                stop_event=stop,
                max_completed_messages=1,
                initial_offset=-1,
            )
            assert completed == 1
            assert [message.text for message in handler.messages] == ["hello v2"]
            assert gateway.calls[0] == -1
            assert inbox.pending() == ()
        finally:
            inbox.close()

    asyncio.run(scenario())


def test_inbox_recovers_processing_message_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite"
    message = IncomingMessage(
        chat_id=10,
        user_id=10,
        message_id=9,
        text="recover me",
        received_at=datetime.now(UTC),
    )
    first = TelegramInboxStore(path)
    assert first.enqueue(88, message) is True
    assert first.mark_processing(88) is True
    first.close()

    second = TelegramInboxStore(path)
    try:
        pending = second.pending()
        assert len(pending) == 1
        assert pending[0].update_id == 88
        assert pending[0].message.text == "recover me"
    finally:
        second.close()
