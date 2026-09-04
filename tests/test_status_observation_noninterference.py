import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.character import SQLiteCharacterStateStore
from amadeus_bot.memory import StructuredMemoryRepository
from amadeus_bot.runtime import (
    ConversationSessionStore,
    SQLiteRuntimePreferenceStore,
)
from amadeus_bot.telegram import IncomingMessage, V2TelegramMessageRouter


class FakeGateway:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> int:
        self.messages.append((chat_id, text))
        return len(self.messages)

    async def send_typing(self, chat_id: int) -> None:
        return None


class FakeConversation:
    def __init__(self, sessions: ConversationSessionStore) -> None:
        self.sessions = sessions

    def has_active_turn(self, chat_id: int) -> bool:
        return False

    def start_new_conversation(self, chat_id: int) -> None:
        self.sessions.rotate(chat_id)

    async def cancel(self, chat_id: int) -> bool:
        return False


class FakeDelivery:
    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        raise AssertionError("observation command must not enter normal delivery")


def _message(text: str, message_id: int) -> IncomingMessage:
    from datetime import UTC, datetime

    return IncomingMessage(
        chat_id=10,
        user_id=10,
        message_id=message_id,
        text=text,
        received_at=datetime.now(UTC),
    )


def test_status_and_autonomy_status_do_not_cancel_pending_spontaneity(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        conversation = FakeConversation(sessions)
        router = V2TelegramMessageRouter(
            gateway=gateway,
            delivery=cast(Any, FakeDelivery()),
            conversation=cast(Any, conversation),
            memory=memory,
            state=state,
            sessions=sessions,
            preferences=preferences,
        )
        pending = asyncio.create_task(asyncio.sleep(60))
        router._spontaneity_tasks[10] = pending
        try:
            await router.handle(_message("/status", 1))
            assert router._spontaneity_tasks.get(10) is pending
            assert not pending.done()

            await router.handle(_message("/autonomy status", 2))
            assert router._spontaneity_tasks.get(10) is pending
            assert not pending.done()

            await router.handle(_message("/autonomy dnd on", 3))
            await asyncio.sleep(0)
            assert 10 not in router._spontaneity_tasks
            assert pending.cancelled()
        finally:
            await router.aclose()
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())
