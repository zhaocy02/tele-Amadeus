import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from amadeus_bot.character import (
    AutonomyOpportunityScheduler,
    AutonomyPlanner,
    SQLiteCharacterStateStore,
)
from amadeus_bot.llm import LLMRequest, LLMResponse
from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    StructuredMemoryRepository,
)
from amadeus_bot.runtime import (
    AutonomyRuntimeCoordinator,
    ConversationSessionStore,
    SQLiteAutonomyRuntimeStore,
    SQLiteRuntimePreferenceStore,
)
from amadeus_bot.telegram import IncomingMessage, V2TelegramMessageRouter


class NeverCalledProvider:
    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("preview must not call the provider")


class FailingPreviewRuntime:
    async def preview(self, chat_id: int, **kwargs: object) -> object:
        raise ValueError("synthetic preview failure")


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


def test_long_memory_is_safely_adapted_into_autonomy_signal(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
        try:
            sessions.append_v2_exchange(
                10,
                "之后还可以继续聊这个。",
                "好。",
                turn_id="turn-long-memory",
                policy_act="DIRECT_ANSWER",
            )
            last_user = sessions.last_user_message_at(10)
            assert last_user is not None
            original_content = "一条很长的真实记忆。" + ("这是需要保留的上下文。" * 80)
            original_id = "memory-" + ("x" * 300)
            await memory.upsert(
                MemoryRecord(
                    memory_id=original_id,
                    kind=MemoryKind.EPISODE,
                    content=original_content,
                    confidence=0.9,
                    salience=0.95,
                    created_at=last_user,
                    updated_at=last_user,
                    source_message_ids=(),
                    source_type=MemorySourceType.MANUAL,
                )
            )
            runtime = AutonomyRuntimeCoordinator(
                planner=AutonomyPlanner(provider=NeverCalledProvider()),
                scheduler=AutonomyOpportunityScheduler(),
                memory_repository=memory,
                state_store=state,
                sessions=sessions,
                autonomy_store=autonomy_store,
            )

            preview = await runtime.preview(10, at=last_user + timedelta(hours=3))

            assert preview.strongest_signal is not None
            signal = preview.strongest_signal
            assert len(signal.summary) <= 500
            assert signal.summary.endswith("…")
            assert len(signal.signal_id) <= 120
            assert signal.source_memory_id is not None
            assert len(signal.source_memory_id) <= 160
            assert original_content.endswith("这是需要保留的上下文。")
            assert len(original_content) > len(signal.summary)
        finally:
            memory.close()
            state.close()
            sessions.close()
            autonomy_store.close()

    asyncio.run(scenario())


def test_status_keeps_last_user_time_and_exposes_preview_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        gateway = FakeGateway()
        router = V2TelegramMessageRouter(
            gateway=cast(Any, gateway),
            delivery=cast(Any, object()),
            conversation=cast(Any, FakeConversation(sessions)),
            memory=memory,
            state=state,
            sessions=sessions,
            preferences=preferences,
            autonomy=cast(Any, FailingPreviewRuntime()),
        )
        try:
            sessions.append_v2_exchange(
                10,
                "刚刚说过的话。",
                "嗯。",
                turn_id="turn-status-fallback",
                policy_act="SHORT_ANSWER",
            )
            message = IncomingMessage(
                chat_id=10,
                user_id=10,
                message_id=1,
                text="/status",
                received_at=datetime.now(UTC),
            )
            await router.handle(message)
            human = gateway.messages[-1][1]
            assert "距离你上次说话：暂无" not in human
            assert "主动联系倾向：暂时无法计算" in human
            assert "autonomy preview 不可用" in human

            debug = IncomingMessage(
                chat_id=10,
                user_id=10,
                message_id=2,
                text="/status debug",
                received_at=datetime.now(UTC),
            )
            await router.handle(debug)
            assert "autonomy_preview_error=synthetic preview failure" in gateway.messages[-1][1]
        finally:
            await router.aclose()
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())
