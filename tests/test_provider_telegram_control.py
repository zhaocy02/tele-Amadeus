import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.character import SQLiteCharacterStateStore
from amadeus_bot.llm import LLMRequest, LLMResponse, ProviderProfile, ProviderRegistry
from amadeus_bot.memory import StructuredMemoryRepository
from amadeus_bot.runtime import (
    ConversationSessionStore,
    RuntimeProviderControl,
    SQLiteProviderPreferenceStore,
    SQLiteRuntimePreferenceStore,
)
from amadeus_bot.telegram import IncomingMessage, V2TelegramMessageRouter


class FakeLLM:
    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="ok", model=request.model or "fake")

    async def aclose(self) -> None:
        return None


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
        self.active = False
        self.cancelled = False

    def has_active_turn(self, chat_id: int) -> bool:
        return self.active

    async def cancel(self, chat_id: int) -> bool:
        del chat_id
        if not self.active:
            return False
        self.active = False
        self.cancelled = True
        return True

    def start_new_conversation(self, chat_id: int) -> None:
        self.sessions.rotate(chat_id)


class FakeDelivery:
    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        del message
        timing = SimpleNamespace(
            policy_ms=0,
            context_ms=0,
            generation_ms=1,
            total_ms=1,
            policy_mode="fast",
        )
        prepared = SimpleNamespace(retrieval_ms=0, turn_result=SimpleNamespace(timing=timing))
        return SimpleNamespace(
            warnings=(),
            prepared=prepared,
            time_to_send_ms=1,
            finalize_ms=0,
        )


def _message(text: str, *, message_id: int = 1) -> IncomingMessage:
    return IncomingMessage(
        chat_id=10,
        user_id=10,
        message_id=message_id,
        text=text,
        received_at=datetime.now(UTC),
    )


def _control(tmp_path: Path) -> tuple[RuntimeProviderControl, SQLiteProviderPreferenceStore]:
    registry = ProviderRegistry(
        (
            ProviderProfile(
                name="cpa",
                display_name="CPA/Codex",
                provider=FakeLLM(),
                text_model="gpt",
                vision_model="gpt",
            ),
            ProviderProfile(
                name="deepseek",
                display_name="DeepSeek",
                provider=FakeLLM(),
                text_model="deepseek-v4-pro",
                vision_model="deepseek-v4-flash-vision-exp",
            ),
        ),
        default_provider="cpa",
    )
    preferences = SQLiteProviderPreferenceStore(tmp_path / "providers.sqlite")
    return (
        RuntimeProviderControl(
            llm_registry=registry,
            preferences=preferences,
        ),
        preferences,
    )


def test_provider_command_switches_locally_and_cancels_active_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "runtime-preferences.sqlite")
        control, provider_preferences = _control(tmp_path)
        conversation = FakeConversation(sessions)
        conversation.active = True
        router = V2TelegramMessageRouter(
            provider_control=control,
            gateway=gateway,
            delivery=cast(Any, FakeDelivery()),
            conversation=cast(Any, conversation),
            memory=memory,
            state=state,
            sessions=sessions,
            preferences=preferences,
        )
        try:
            await router.handle(_message("/provider deepseek"))
            assert conversation.cancelled is True
            assert provider_preferences.get(10).llm_provider == "deepseek"
            assert "deepseek-v4-pro" in gateway.messages[-1][1]

            await router.handle(_message("/provider", message_id=2))
            assert "LLM=deepseek" in gateway.messages[-1][1]
            assert "automatic_failover=off" in gateway.messages[-1][1]
        finally:
            await router.aclose()
            memory.close()
            state.close()
            sessions.close()
            preferences.close()
            provider_preferences.close()

    asyncio.run(scenario())


def test_exact_natural_language_switch_is_control_traffic(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "runtime-preferences.sqlite")
        control, provider_preferences = _control(tmp_path)
        conversation = FakeConversation(sessions)
        delivery = FakeDelivery()
        router = V2TelegramMessageRouter(
            provider_control=control,
            gateway=gateway,
            delivery=cast(Any, delivery),
            conversation=cast(Any, conversation),
            memory=memory,
            state=state,
            sessions=sessions,
            preferences=preferences,
        )
        try:
            await router.handle(_message("切到 DeepSeek"))
            assert provider_preferences.get(10).llm_provider == "deepseek"

            await router.handle(_message("你觉得切到 DeepSeek 怎么样", message_id=2))
            assert provider_preferences.get(10).llm_provider == "deepseek"
        finally:
            await router.aclose()
            memory.close()
            state.close()
            sessions.close()
            preferences.close()
            provider_preferences.close()

    asyncio.run(scenario())
