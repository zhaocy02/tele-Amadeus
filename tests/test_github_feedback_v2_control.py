from __future__ import annotations

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
        del chat_id


class FakeConversation:
    def __init__(self, sessions: ConversationSessionStore) -> None:
        self.sessions = sessions

    def has_active_turn(self, chat_id: int) -> bool:
        del chat_id
        return False

    async def cancel(self, chat_id: int) -> bool:
        del chat_id
        return False

    def start_new_conversation(self, chat_id: int) -> None:
        self.sessions.rotate(chat_id)


class FakeDelivery:
    def __init__(self) -> None:
        self.user_messages = 0

    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        del message
        self.user_messages += 1
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


class FakeGitHubCommand:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def handle(self, chat_id: int, argument: str) -> None:
        self.calls.append((chat_id, argument))


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
        ),
        default_provider="cpa",
    )
    preferences = SQLiteProviderPreferenceStore(tmp_path / "providers.sqlite")
    return RuntimeProviderControl(llm_registry=registry, preferences=preferences), preferences


def _router_fixture(
    tmp_path: Path,
    *,
    github_feedback: FakeGitHubCommand | None,
) -> tuple[
    V2TelegramMessageRouter,
    FakeGateway,
    FakeDelivery,
    tuple[object, ...],
]:
    gateway = FakeGateway()
    delivery = FakeDelivery()
    memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
    state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    preferences = SQLiteRuntimePreferenceStore(tmp_path / "runtime-preferences.sqlite")
    control, provider_preferences = _control(tmp_path)
    conversation = FakeConversation(sessions)
    router = V2TelegramMessageRouter(
        provider_control=control,
        github_feedback=cast(Any, github_feedback),
        gateway=gateway,
        delivery=cast(Any, delivery),
        conversation=cast(Any, conversation),
        memory=memory,
        state=state,
        sessions=sessions,
        preferences=preferences,
    )
    resources: tuple[object, ...] = (
        memory,
        state,
        sessions,
        preferences,
        provider_preferences,
    )
    return router, gateway, delivery, resources


def _close_resources(resources: tuple[object, ...]) -> None:
    for resource in resources:
        cast(Any, resource).close()


def test_github_command_routes_only_to_explicit_feedback_control(tmp_path: Path) -> None:
    async def scenario() -> None:
        github = FakeGitHubCommand()
        router, gateway, delivery, resources = _router_fixture(
            tmp_path,
            github_feedback=github,
        )
        try:
            await router.handle(_message("/github pending"))

            assert github.calls == [(10, "pending")]
            assert delivery.user_messages == 0
            assert gateway.messages == []
        finally:
            await router.aclose()
            _close_resources(resources)

    asyncio.run(scenario())


def test_github_command_fails_closed_when_capability_is_not_composed(tmp_path: Path) -> None:
    async def scenario() -> None:
        router, gateway, delivery, resources = _router_fixture(
            tmp_path,
            github_feedback=None,
        )
        try:
            await router.handle(_message("/github"))

            assert delivery.user_messages == 0
            assert gateway.messages[-1][1] == "Public GitHub Feedback 当前未启用。"
        finally:
            await router.aclose()
            _close_resources(resources)

    asyncio.run(scenario())


def test_help_exposes_github_feedback_control_without_changing_normal_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        github = FakeGitHubCommand()
        router, gateway, delivery, resources = _router_fixture(
            tmp_path,
            github_feedback=github,
        )
        try:
            await router.handle(_message("/help"))
            assert "/github - public GitHub feedback proposal/confirm" in gateway.messages[-1][1]

            await router.handle(_message("普通聊天", message_id=2))
            assert delivery.user_messages == 1
            assert github.calls == []
        finally:
            await router.aclose()
            _close_resources(resources)

    asyncio.run(scenario())
