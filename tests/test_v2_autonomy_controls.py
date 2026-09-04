import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from amadeus_bot.character import (
    AutonomyAction,
    AutonomyDecision,
    AutonomyOpportunity,
    AutonomySignal,
    AutonomySignalKind,
    SQLiteCharacterStateStore,
)
from amadeus_bot.memory import StructuredMemoryRepository
from amadeus_bot.runtime import (
    AutonomyRuntimeEvaluation,
    ConversationSessionStore,
    SQLiteAutonomyRuntimeStore,
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
        self.active = False

    def has_active_turn(self, chat_id: int) -> bool:
        return self.active

    def start_new_conversation(self, chat_id: int) -> None:
        self.sessions.rotate(chat_id)

    async def cancel(self, chat_id: int) -> bool:
        return False


class FakeDelivery:
    def __init__(self) -> None:
        self.autonomy_calls: list[tuple[AutonomyRuntimeEvaluation, str]] = []

    async def deliver_prepared_autonomy_message(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        text: str,
    ) -> SimpleNamespace:
        self.autonomy_calls.append((evaluation, text))
        return SimpleNamespace(telegram_message_id=99, warnings=())

    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        timing = SimpleNamespace(
            policy_ms=0,
            context_ms=1,
            generation_ms=1,
            total_ms=2,
            policy_mode="fast",
        )
        prepared = SimpleNamespace(retrieval_ms=1, turn_result=SimpleNamespace(timing=timing))
        return SimpleNamespace(
            warnings=(),
            prepared=prepared,
            time_to_send_ms=3,
            finalize_ms=1,
        )


def _message(text: str, message_id: int = 1) -> IncomingMessage:
    return IncomingMessage(
        chat_id=10,
        user_id=10,
        message_id=message_id,
        text=text,
        received_at=datetime.now(UTC),
    )


def _evaluation() -> AutonomyRuntimeEvaluation:
    now = datetime.now(UTC)
    signal = AutonomySignal(
        signal_id="open-thread:test",
        kind=AutonomySignalKind.OPEN_THREAD,
        summary="之前有个话题还可以继续。",
        salience=0.9,
        source_thread_id="test",
    )
    return AutonomyRuntimeEvaluation(
        chat_id=10,
        generation=1,
        evaluated_at=now,
        due=True,
        opportunity=AutonomyOpportunity(
            now=now,
            last_user_message_at=now - timedelta(hours=4),
            signals=(signal,),
        ),
        decision=AutonomyDecision(
            action=AutonomyAction.FOLLOW_UP,
            selected_signal_id=signal.signal_id,
            motivation=0.9,
            focus="接回这个话题",
            reason_label="grounded_follow_up",
        ),
    )


def _build_router(tmp_path: Path, *, runtime_gate: bool) -> tuple[
    FakeGateway,
    FakeDelivery,
    StructuredMemoryRepository,
    SQLiteCharacterStateStore,
    ConversationSessionStore,
    SQLiteRuntimePreferenceStore,
    SQLiteAutonomyRuntimeStore,
    V2TelegramMessageRouter,
]:
    gateway = FakeGateway()
    delivery = FakeDelivery()
    memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
    state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
    autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
    conversation = FakeConversation(sessions)
    router = V2TelegramMessageRouter(
        gateway=cast(Any, gateway),
        delivery=cast(Any, delivery),
        conversation=cast(Any, conversation),
        memory=memory,
        state=state,
        sessions=sessions,
        preferences=preferences,
        autonomy_store=autonomy_store,
        autonomy_pilot_enabled=runtime_gate,
        autonomy_timezone="Asia/Shanghai",
    )
    return (
        gateway,
        delivery,
        memory,
        state,
        sessions,
        preferences,
        autonomy_store,
        router,
    )


def test_autonomy_commands_are_default_off_and_persistent(tmp_path: Path) -> None:
    async def scenario() -> None:
        built = _build_router(tmp_path, runtime_gate=False)
        gateway, _, memory, state, sessions, preferences, autonomy_store, router = built
        try:
            await router.handle(_message("/autonomy"))
            status = gateway.messages[-1][1]
            assert "chat_opt_in=off" in status
            assert "runtime_gate=off" in status
            assert "sleep_mode=off" in status
            assert "sleep_cap=2" in status
            assert "opportunity:5m idle:3m" in status
            assert "max:24/24h" in status
            assert "max:unlimited" in status

            await router.handle(_message("/autonomy on", 2))
            assert preferences.autonomy_preferences(10).enabled is True
            assert "进程级 gate 仍关闭" in gateway.messages[-1][1]

            await router.handle(_message("/autonomy dnd on", 3))
            assert preferences.autonomy_preferences(10).do_not_disturb is True

            old_window = preferences.autonomy_preferences(10).quiet_window
            await router.handle(_message("/autonomy quiet 23:30-07:15", 4))
            assert preferences.autonomy_preferences(10).quiet_window == old_window
            assert "固定时间 quiet hours 已停用" in gateway.messages[-1][1]
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()
            autonomy_store.close()

    asyncio.run(scenario())


def test_runtime_gate_blocks_final_autonomy_send(tmp_path: Path) -> None:
    async def scenario() -> None:
        built = _build_router(tmp_path, runtime_gate=False)
        _, delivery, memory, state, sessions, preferences, autonomy_store, router = built
        try:
            preferences.set_autonomy_enabled(10, True)
            with pytest.raises(ValueError, match="process gate"):
                await router.deliver_prepared_autonomy(_evaluation(), "主动消息")
            assert delivery.autonomy_calls == []
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()
            autonomy_store.close()

    asyncio.run(scenario())


def test_queued_user_input_has_priority_over_autonomy(tmp_path: Path) -> None:
    async def scenario() -> None:
        built = _build_router(tmp_path, runtime_gate=True)
        _, delivery, memory, state, sessions, preferences, autonomy_store, router = built
        try:
            preferences.set_autonomy_enabled(10, True)
            lock = router._chat_lock(10)
            await lock.acquire()
            autonomy_task = asyncio.create_task(
                router.deliver_prepared_autonomy(_evaluation(), "主动消息")
            )
            await asyncio.sleep(0)
            user_task = asyncio.create_task(router.handle(_message("用户现在说话了", 5)))
            await asyncio.sleep(0)
            lock.release()

            with pytest.raises(ValueError, match="queued user input"):
                await autonomy_task
            await user_task
            assert delivery.autonomy_calls == []
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()
            autonomy_store.close()

    asyncio.run(scenario())
