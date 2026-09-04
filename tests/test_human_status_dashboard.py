import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.character import (
    AutonomyGuard,
    AutonomyGuardConfig,
    AutonomyOpportunityScheduler,
    AutonomyPlanner,
    AutonomyScheduleConfig,
    SpontaneityConfig,
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
    SQLiteSpontaneityStore,
)
from amadeus_bot.telegram import IncomingMessage, V2TelegramMessageRouter


class NeverCalledProvider:
    async def generate(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("status preview must not call the LLM provider")


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

    def has_active_turn(self, chat_id: int) -> bool:
        return self.active

    def start_new_conversation(self, chat_id: int) -> None:
        self.sessions.rotate(chat_id)

    async def cancel(self, chat_id: int) -> bool:
        return False


class FakeDelivery:
    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        raise AssertionError("status commands must not enter normal delivery")


def _runtime(
    tmp_path: Path,
) -> tuple[
    StructuredMemoryRepository,
    SQLiteCharacterStateStore,
    ConversationSessionStore,
    SQLiteRuntimePreferenceStore,
    SQLiteAutonomyRuntimeStore,
    SQLiteSpontaneityStore,
    AutonomyRuntimeCoordinator,
]:
    memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
    state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
    autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
    spontaneity_store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
    guard = AutonomyGuard(
        AutonomyGuardConfig(
            min_user_idle=timedelta(minutes=3),
            proactive_cooldown=timedelta(minutes=30),
            max_messages_per_24h=24,
            max_consecutive_unanswered=3,
            max_messages_per_sleep_session=2,
            min_signal_salience=0.50,
            min_model_motivation=0.50,
            idle_drive_max_motivation_bonus=0.25,
        )
    )
    autonomy = AutonomyRuntimeCoordinator(
        planner=AutonomyPlanner(provider=NeverCalledProvider(), guard=guard),
        scheduler=AutonomyOpportunityScheduler(
            AutonomyScheduleConfig(check_interval=timedelta(minutes=5))
        ),
        memory_repository=memory,
        state_store=state,
        sessions=sessions,
        autonomy_store=autonomy_store,
    )
    return (
        memory,
        state,
        sessions,
        preferences,
        autonomy_store,
        spontaneity_store,
        autonomy,
    )


async def _seed(memory: StructuredMemoryRepository, sessions: ConversationSessionStore) -> None:
    sessions.append_v2_exchange(
        10,
        "之后我们继续聊这个主动消息机制。",
        "好。",
        turn_id="turn-status-1",
        policy_act="DIRECT_ANSWER",
    )
    last_user = sessions.last_user_message_at(10)
    assert last_user is not None
    await memory.upsert(
        MemoryRecord(
            memory_id="thread_status",
            kind=MemoryKind.OPEN_THREAD,
            content="用户想继续讨论 Amadeus 的主动消息机制和自然程度。",
            confidence=0.95,
            salience=0.90,
            created_at=last_user,
            updated_at=last_user,
            source_message_ids=(),
            source_type=MemorySourceType.MANUAL,
        )
    )


def test_autonomy_preview_is_read_only_and_exposes_human_tuning_metrics(tmp_path: Path) -> None:
    async def scenario() -> None:
        (
            memory,
            state,
            sessions,
            preferences,
            autonomy_store,
            spontaneity_store,
            autonomy,
        ) = _runtime(tmp_path)
        try:
            await _seed(memory, sessions)
            last_user = sessions.last_user_message_at(10)
            assert last_user is not None
            observed_at = last_user + timedelta(hours=8)

            assert autonomy_store.last_opportunity_at(10, 1) is None
            assert autonomy_store.last_evaluation(10, 1) is None

            preview = await autonomy.preview(10, at=observed_at)

            assert preview.idle_contact_drive == 0.95
            assert preview.raw_motivation_threshold == 0.2625
            assert preview.strongest_signal is not None
            assert preview.strongest_signal.signal_id == "open-thread:thread_status"
            assert 0.89 < preview.strongest_signal.salience < 0.90
            expected_urge = round(
                0.55 * preview.idle_contact_drive + 0.45 * preview.strongest_signal.salience,
                4,
            )
            assert preview.contact_urge == expected_urge
            assert preview.guard_allowed is True
            assert preview.guard_reason_label == "guard_pass"
            assert preview.opportunity_interval == timedelta(minutes=5)
            assert preview.next_opportunity_at == observed_at

            assert autonomy_store.last_opportunity_at(10, 1) is None
            assert autonomy_store.last_evaluation(10, 1) is None
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()
            autonomy_store.close()
            spontaneity_store.close()

    asyncio.run(scenario())


def test_status_defaults_to_human_dashboard_and_debug_keeps_machine_view(tmp_path: Path) -> None:
    async def scenario() -> None:
        (
            memory,
            state,
            sessions,
            preferences,
            autonomy_store,
            spontaneity_store,
            autonomy,
        ) = _runtime(tmp_path)
        gateway = FakeGateway()
        conversation = FakeConversation(sessions)
        composer = SimpleNamespace(config=SpontaneityConfig())
        router = V2TelegramMessageRouter(
            gateway=gateway,
            delivery=cast(Any, FakeDelivery()),
            conversation=cast(Any, conversation),
            memory=memory,
            state=state,
            sessions=sessions,
            preferences=preferences,
            autonomy=autonomy,
            autonomy_store=autonomy_store,
            autonomy_pilot_enabled=True,
            spontaneity_composer=cast(Any, composer),
            spontaneity_store=spontaneity_store,
            spontaneity_process_enabled=True,
        )
        try:
            await _seed(memory, sessions)
            preferences.set_autonomy_enabled(10, True)
            preferences.set_spontaneity_enabled(10, True)
            last_user = sessions.last_user_message_at(10)
            assert last_user is not None

            message = IncomingMessage(
                chat_id=10,
                user_id=10,
                message_id=100,
                text="/status",
                received_at=last_user,
            )
            await router.handle(message)
            human = gateway.messages[-1][1]

            assert human.startswith("Amadeus 状态")
            assert "主动联系倾向：" in human
            assert "想联系你：" in human
            assert "有话想说：" in human
            assert "想主动找你：" not in human
            assert "沉默驱动力：" not in human
            assert "当前最想提起的话题：" in human
            assert "主动开口门槛：" in human
            assert "下一次主动思考：" in human
            assert "当前为什么还没找你：" in human
            assert "机器诊断：/status debug" in human
            assert "conversation_generation=" not in human

            debug_message = IncomingMessage(
                chat_id=10,
                user_id=10,
                message_id=101,
                text="/status debug",
                received_at=last_user,
            )
            await router.handle(debug_message)
            debug = gateway.messages[-1][1]
            assert "conversation_generation=1" in debug
            assert "autonomy_idle_contact_drive=" in debug
            assert "autonomy_contact_urge=" in debug
            assert "autonomy_raw_motivation_threshold=" in debug
            assert (
                "spontaneity_live_policy=first:1-30s chain:3-15s "
                "cooldown:2m max:unlimited"
            ) in debug

            autonomy_message = IncomingMessage(
                chat_id=10,
                user_id=10,
                message_id=102,
                text="/autonomy",
                received_at=last_user,
            )
            await router.handle(autonomy_message)
            autonomy_text = gateway.messages[-1][1]
            assert "policy=opportunity:5m idle:3m base_cooldown:30m max:24/24h" in autonomy_text
            assert (
                "spontaneity_policy=first:1-30s chain:3-15s cooldown:2m "
                "max:unlimited episode-cap:7"
            ) in autonomy_text
        finally:
            await router.aclose()
            memory.close()
            state.close()
            sessions.close()
            preferences.close()
            autonomy_store.close()
            spontaneity_store.close()

    asyncio.run(scenario())
