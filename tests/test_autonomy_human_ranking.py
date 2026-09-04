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


def _runtime(
    memory: StructuredMemoryRepository,
    state: SQLiteCharacterStateStore,
    sessions: ConversationSessionStore,
    autonomy_store: SQLiteAutonomyRuntimeStore,
) -> AutonomyRuntimeCoordinator:
    return AutonomyRuntimeCoordinator(
        planner=AutonomyPlanner(provider=NeverCalledProvider()),
        scheduler=AutonomyOpportunityScheduler(),
        memory_repository=memory,
        state_store=state,
        sessions=sessions,
        autonomy_store=autonomy_store,
    )


def _memory(
    *,
    memory_id: str,
    kind: MemoryKind,
    content: str,
    salience: float,
    updated_at: datetime,
    last_recalled_at: datetime | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        kind=kind,
        content=content,
        confidence=0.9,
        salience=salience,
        created_at=updated_at,
        updated_at=updated_at,
        last_recalled_at=last_recalled_at,
        source_message_ids=(),
        source_type=MemorySourceType.MANUAL,
    )


def test_fresh_open_thread_outranks_stale_high_salience_episode(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
        try:
            sessions.append_v2_exchange(
                10,
                "你好呀",
                "嗯。",
                turn_id="turn-ranking",
                policy_act="SHORT_ANSWER",
            )
            last_user = sessions.last_user_message_at(10)
            assert last_user is not None
            observed_at = last_user + timedelta(hours=3)
            await memory.upsert(
                _memory(
                    memory_id="legacy-episode",
                    kind=MemoryKind.EPISODE,
                    content="一条很重要但已经很久以前的经历",
                    salience=0.99,
                    updated_at=observed_at - timedelta(days=60),
                )
            )
            await memory.upsert(
                _memory(
                    memory_id="fresh-thread",
                    kind=MemoryKind.OPEN_THREAD,
                    content="刚留下的、还没有聊完的话题",
                    salience=0.76,
                    updated_at=observed_at - timedelta(hours=1),
                )
            )

            preview = await _runtime(memory, state, sessions, autonomy_store).preview(
                10,
                at=observed_at,
            )
            by_summary = {signal.summary: signal.salience for signal in preview.opportunity.signals}

            assert by_summary["刚留下的、还没有聊完的话题"] > by_summary[
                "一条很重要但已经很久以前的经历"
            ]
            assert preview.strongest_signal is not None
            assert preview.strongest_signal.summary == "刚留下的、还没有聊完的话题"
        finally:
            memory.close()
            state.close()
            sessions.close()
            autonomy_store.close()

    asyncio.run(scenario())


def test_recently_recalled_memory_gets_temporary_reuse_penalty(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
        try:
            sessions.append_v2_exchange(
                10,
                "你好呀",
                "嗯。",
                turn_id="turn-reuse",
                policy_act="SHORT_ANSWER",
            )
            last_user = sessions.last_user_message_at(10)
            assert last_user is not None
            observed_at = last_user + timedelta(hours=3)
            await memory.upsert(
                _memory(
                    memory_id="recently-used",
                    kind=MemoryKind.EPISODE,
                    content="刚刚才被拿出来说过的高显著记忆",
                    salience=0.95,
                    updated_at=observed_at - timedelta(hours=1),
                    last_recalled_at=observed_at - timedelta(hours=1),
                )
            )
            await memory.upsert(
                _memory(
                    memory_id="unused",
                    kind=MemoryKind.EPISODE,
                    content="还没有反复拿出来说的记忆",
                    salience=0.75,
                    updated_at=observed_at - timedelta(hours=1),
                )
            )

            preview = await _runtime(memory, state, sessions, autonomy_store).preview(
                10,
                at=observed_at,
            )
            by_summary = {signal.summary: signal.salience for signal in preview.opportunity.signals}

            assert by_summary["还没有反复拿出来说的记忆"] > by_summary[
                "刚刚才被拿出来说过的高显著记忆"
            ]
        finally:
            memory.close()
            state.close()
            sessions.close()
            autonomy_store.close()

    asyncio.run(scenario())


def test_status_separates_contact_desire_from_topic_impulse(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        gateway = FakeGateway()
        runtime = _runtime(memory, state, sessions, autonomy_store)
        router = V2TelegramMessageRouter(
            gateway=cast(Any, gateway),
            delivery=cast(Any, object()),
            conversation=cast(Any, FakeConversation(sessions)),
            memory=memory,
            state=state,
            sessions=sessions,
            preferences=preferences,
            autonomy_store=autonomy_store,
            autonomy=runtime,
            autonomy_pilot_enabled=True,
        )
        try:
            sessions.append_v2_exchange(
                10,
                "你好呀",
                "嗯。",
                turn_id="turn-human-status",
                policy_act="SHORT_ANSWER",
            )
            now = datetime.now(UTC)
            await memory.upsert(
                _memory(
                    memory_id="status-thread",
                    kind=MemoryKind.OPEN_THREAD,
                    content="还没聊完的测试话题",
                    salience=0.8,
                    updated_at=now,
                )
            )
            preferences.set_autonomy_enabled(10, True)

            await router.handle(
                IncomingMessage(
                    chat_id=10,
                    user_id=10,
                    message_id=1,
                    text="/status",
                    received_at=now,
                )
            )
            text = gateway.messages[-1][1]

            assert "主动联系倾向：" in text
            assert "想联系你：" in text
            assert "有话想说：" in text
            assert "当前最想提起的话题：" in text
            assert "沉默驱动力：" not in text
            assert "想主动找你：" not in text
        finally:
            await router.aclose()
            memory.close()
            state.close()
            sessions.close()
            autonomy_store.close()
            preferences.close()

    asyncio.run(scenario())
