import asyncio
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.character import LegacyPersona
from amadeus_bot.llm import LLMRequest, LLMResponse
from amadeus_bot.memory import LegacyMemoryStore
from amadeus_bot.runtime import ConversationSessionStore, LegacyConversationService
from amadeus_bot.telegram import (
    HELP_TEXT,
    IncomingMessage,
    TelegramMessageRouter,
    parse_authorized_text_update,
)


class FakeGateway:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.typing: list[int] = []

    async def send_message(self, chat_id: int, text: str) -> int:
        self.messages.append((chat_id, text))
        return len(self.messages)

    async def send_typing(self, chat_id: int) -> None:
        self.typing.append(chat_id)


class FixedProvider:
    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=f"reply:{request.messages[-1].content}", model="test")


def _message(text: str) -> IncomingMessage:
    return IncomingMessage(
        chat_id=10,
        user_id=10,
        message_id=1,
        text=text,
        received_at=datetime.now(UTC),
    )


def _router(
    tmp_path: Path,
) -> tuple[TelegramMessageRouter, FakeGateway, LegacyMemoryStore, ConversationSessionStore]:
    gateway = FakeGateway()
    memory = LegacyMemoryStore(tmp_path / "memory.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    conversation = LegacyConversationService(
        provider=FixedProvider(),
        persona=LegacyPersona("persona", "hash"),
        memory=memory,
        sessions=sessions,
        model="test",
    )
    return TelegramMessageRouter(gateway, conversation), gateway, memory, sessions


def test_update_parser_accepts_only_allowlisted_private_text() -> None:
    update = {
        "message": {
            "message_id": 5,
            "date": 1_700_000_000,
            "text": "hello",
            "chat": {"id": 10, "type": "private"},
            "from": {"id": 10},
        }
    }
    assert parse_authorized_text_update(update, {10}) is not None
    assert parse_authorized_text_update(update, {11}) is None

    group_update = {"message": {**update["message"], "chat": {"id": 10, "type": "group"}}}
    assert parse_authorized_text_update(group_update, {10}) is None


def test_router_preserves_help_memory_new_and_plain_text_behavior(tmp_path: Path) -> None:
    async def scenario() -> None:
        router, gateway, memory, sessions = _router(tmp_path)
        try:
            await router.handle(_message("/help"))
            assert gateway.messages[-1] == (10, HELP_TEXT)

            await router.handle(_message("/remember 我喜欢咖啡"))
            assert "记住了" in gateway.messages[-1][1]

            await router.handle(_message("/memory"))
            assert "我喜欢咖啡" in gateway.messages[-1][1]

            await router.handle(_message("你好"))
            assert gateway.messages[-1] == (10, "reply:你好")

            await router.handle(_message("/new"))
            assert gateway.messages[-1][1] == "好，我们开始一段新对话。长期记忆仍会保留。"
            await router.handle(_message("/history"))
            assert gateway.messages[-1][1] == "当前对话还没有可显示的历史记录。"

            await router.handle(_message("/memory off"))
            assert "长期记忆已关闭" in gateway.messages[-1][1]
            assert not memory.is_enabled(10)
        finally:
            memory.close()
            sessions.close()

    asyncio.run(scenario())
