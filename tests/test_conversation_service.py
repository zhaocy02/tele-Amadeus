import asyncio
from pathlib import Path

import pytest

from amadeus_bot.character import LegacyPersona
from amadeus_bot.llm import LLMRequest, LLMResponse, MessageRole
from amadeus_bot.memory import LegacyMemoryKind, LegacyMemoryStore
from amadeus_bot.runtime import (
    ConversationCancelledError,
    ConversationSessionStore,
    LegacyConversationService,
)


class RecordingProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text="response", model=request.model or "test")


class BlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.started.set()
        await asyncio.Event().wait()
        return LLMResponse(text="unreachable", model=request.model or "test")


def _service(
    tmp_path: Path, provider: RecordingProvider | BlockingProvider
) -> tuple[LegacyConversationService, LegacyMemoryStore, ConversationSessionStore]:
    memory = LegacyMemoryStore(tmp_path / "memory.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    service = LegacyConversationService(
        provider=provider,
        persona=LegacyPersona("persona baseline", "hash"),
        memory=memory,
        sessions=sessions,
        model="gpt-test",
    )
    return service, memory, sessions


def test_conversation_injects_memory_preserves_history_and_auto_remembers(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = RecordingProvider()
        service, memory, sessions = _service(tmp_path, provider)
        try:
            memory.remember(
                9,
                "我喜欢咖啡",
                kind=LegacyMemoryKind.PREFERENCE,
            )
            assert await service.reply(9, "咖啡怎么样") == "response"
            first = provider.requests[0]
            assert first.messages[0].role is MessageRole.DEVELOPER
            assert "persona baseline" in first.messages[0].content
            assert "我喜欢咖啡" in first.messages[0].content

            await service.reply(9, "我计划明天跑步")
            second = provider.requests[1]
            assert [message.role for message in second.messages[-3:]] == [
                MessageRole.USER,
                MessageRole.ASSISTANT,
                MessageRole.USER,
            ]
            assert any(
                item.kind is LegacyMemoryKind.COMMITMENT
                for item in memory.list(9, 20)
            )
        finally:
            memory.close()
            sessions.close()

    asyncio.run(scenario())


def test_conversation_cancel_interrupts_active_provider_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = BlockingProvider()
        service, memory, sessions = _service(tmp_path, provider)
        try:
            pending = asyncio.create_task(service.reply(9, "hello"))
            await provider.started.wait()
            assert await service.cancel(9)
            with pytest.raises(ConversationCancelledError):
                await pending
            assert not service.has_active_turn(9)
        finally:
            memory.close()
            sessions.close()

    asyncio.run(scenario())
