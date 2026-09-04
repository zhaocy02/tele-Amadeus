import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.character import (
    ConversationAct,
    SpontaneityAction,
    SpontaneityConfig,
    SpontaneityDecision,
    SQLiteCharacterStateStore,
)
from amadeus_bot.memory import StructuredMemoryRepository
from amadeus_bot.runtime import (
    ConversationSessionStore,
    SQLiteRuntimePreferenceStore,
    SQLiteSpontaneityStore,
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
    def __init__(self, store: SQLiteSpontaneityStore) -> None:
        self.store = store
        self.spontaneity_calls: list[tuple[int, str, str]] = []

    async def deliver_spontaneity_message(
        self,
        *,
        chat_id: int,
        generation: int,
        source_turn_id: str,
        text: str,
        evaluation_id: int | None,
    ) -> SimpleNamespace:
        self.spontaneity_calls.append((chat_id, source_turn_id, text))
        self.store.record_delivery(
            chat_id,
            generation,
            source_turn_id=source_turn_id,
            evaluation_id=evaluation_id,
            at=datetime.now(UTC),
        )
        return SimpleNamespace(telegram_message_id=9002, warnings=())


class FixedComposer:
    def __init__(self, config: SpontaneityConfig | None = None) -> None:
        self.config = config or SpontaneityConfig()
        self.calls = 0

    async def compose(self, opportunity: object) -> SpontaneityDecision:
        self.calls += 1
        return SpontaneityDecision(
            action=SpontaneityAction.CONTINUE,
            motivation=0.8,
            focus="one new edge case",
            reason_label="afterthought",
            text="……等等，我又想到一个边界条件。",
        )


class BlockingComposer(FixedComposer):
    def __init__(self, config: SpontaneityConfig | None = None) -> None:
        super().__init__(config)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def compose(self, opportunity: object) -> SpontaneityDecision:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return SpontaneityDecision(
            action=SpontaneityAction.CONTINUE,
            motivation=0.8,
            focus="late thought",
            reason_label="afterthought",
            text="……等等，我这句还没说完。",
        )


def _message(text: str, message_id: int = 1) -> IncomingMessage:
    return IncomingMessage(
        chat_id=10,
        user_id=10,
        message_id=message_id,
        text=text,
        received_at=datetime.now(UTC),
    )


def _build(
    tmp_path: Path,
    composer: FixedComposer,
) -> tuple[
    FakeGateway,
    FakeDelivery,
    StructuredMemoryRepository,
    SQLiteCharacterStateStore,
    ConversationSessionStore,
    SQLiteRuntimePreferenceStore,
    SQLiteSpontaneityStore,
    V2TelegramMessageRouter,
]:
    gateway = FakeGateway()
    memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
    state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
    spontaneity_store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
    delivery = FakeDelivery(spontaneity_store)
    conversation = FakeConversation(sessions)
    router = V2TelegramMessageRouter(
        gateway=cast(Any, gateway),
        delivery=cast(Any, delivery),
        conversation=cast(Any, conversation),
        memory=memory,
        state=state,
        sessions=sessions,
        preferences=preferences,
        spontaneity_composer=cast(Any, composer),
        spontaneity_store=spontaneity_store,
        spontaneity_process_enabled=True,
    )
    preferences.set_autonomy_enabled(10, True)
    preferences.set_spontaneity_enabled(10, True)
    return (
        gateway,
        delivery,
        memory,
        state,
        sessions,
        preferences,
        spontaneity_store,
        router,
    )


def _source_exchange(sessions: ConversationSessionStore) -> int:
    exchange = sessions.append_v2_exchange(
        10,
        "你确定没有其他边界了吗？",
        "我现在还不能完全确定。",
        turn_id="turn_source",
        policy_act=ConversationAct.ADMIT_UNCERTAINTY.value,
    )
    return exchange.user_message_id


def _close(built: tuple[object, ...]) -> None:
    for item in built[2:7]:
        close = getattr(item, "close", None)
        if close is not None:
            close()


def test_spontaneity_command_is_separate_opt_in(tmp_path: Path) -> None:
    async def scenario() -> None:
        composer = FixedComposer()
        built = _build(tmp_path, composer)
        gateway, _, _, _, _, preferences, _, router = built
        try:
            preferences.set_spontaneity_enabled(10, False)
            await router.handle(_message("/autonomy spontaneity on"))
            assert preferences.autonomy_preferences(10).spontaneity_enabled is True
            assert "短期续话已开启" in gateway.messages[-1][1]

            await router.handle(_message("/autonomy spontaneity off", 2))
            assert preferences.autonomy_preferences(10).spontaneity_enabled is False
        finally:
            await router.aclose()
            _close(built)

    asyncio.run(scenario())


def test_command_cancels_composer_in_flight(tmp_path: Path) -> None:
    async def scenario() -> None:
        composer = BlockingComposer()
        built = _build(tmp_path, composer)
        _, delivery, _, _, sessions, _, store, router = built
        source_user_message_id = _source_exchange(sessions)
        try:
            task = asyncio.create_task(
                router._run_spontaneity(
                    chat_id=10,
                    generation=sessions.current_generation(10),
                    source_user_message_id=source_user_message_id,
                    source_turn_id="turn_source",
                    user_text="你确定没有其他边界了吗？",
                    assistant_text="我现在还不能完全确定。",
                    policy_act=ConversationAct.ADMIT_UNCERTAINTY,
                    delay_seconds=0,
                    allow_interrupt_window=True,
                )
            )
            router._spontaneity_tasks[10] = task
            await composer.started.wait()

            await router.handle(_message("/autonomy dnd on", 3))
            with suppress(asyncio.CancelledError):
                await task

            assert task.cancelled()
            assert delivery.spontaneity_calls == []
            assert store.list_evaluations_since(datetime(2026, 1, 1, tzinfo=UTC)) == ()
        finally:
            await router.aclose()
            _close(built)

    asyncio.run(scenario())


def test_ordinary_user_input_cancels_without_interrupt_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        composer = BlockingComposer()
        built = _build(tmp_path, composer)
        _, delivery, _, _, sessions, _, _, router = built
        source_user_message_id = _source_exchange(sessions)
        try:
            task = asyncio.create_task(
                router._run_spontaneity(
                    chat_id=10,
                    generation=sessions.current_generation(10),
                    source_user_message_id=source_user_message_id,
                    source_turn_id="turn_source",
                    user_text="你确定没有其他边界了吗？",
                    assistant_text="我现在还不能完全确定。",
                    policy_act=ConversationAct.ADMIT_UNCERTAINTY,
                    delay_seconds=0,
                    allow_interrupt_window=False,
                )
            )
            router._spontaneity_tasks[10] = task
            await composer.started.wait()

            await router._arbitrate_spontaneity_before_user(10)
            with suppress(asyncio.CancelledError):
                await task

            assert task.cancelled()
            assert delivery.spontaneity_calls == []
        finally:
            await router.aclose()
            _close(built)

    asyncio.run(scenario())


def test_interrupt_window_can_finish_before_ordinary_user_handling(tmp_path: Path) -> None:
    async def scenario() -> None:
        composer = BlockingComposer(SpontaneityConfig(interrupt_grace_seconds=0.2))
        built = _build(tmp_path, composer)
        _, delivery, _, _, sessions, _, store, router = built
        source_user_message_id = _source_exchange(sessions)
        try:
            task = asyncio.create_task(
                router._run_spontaneity(
                    chat_id=10,
                    generation=sessions.current_generation(10),
                    source_user_message_id=source_user_message_id,
                    source_turn_id="turn_source",
                    user_text="你确定没有其他边界了吗？",
                    assistant_text="我现在还不能完全确定。",
                    policy_act=ConversationAct.ADMIT_UNCERTAINTY,
                    delay_seconds=0,
                    allow_interrupt_window=True,
                )
            )
            router._spontaneity_tasks[10] = task
            await composer.started.wait()

            async def release_soon() -> None:
                await asyncio.sleep(0.01)
                composer.release.set()

            release_task = asyncio.create_task(release_soon())
            await router._arbitrate_spontaneity_before_user(10)
            await release_task
            await task

            assert not task.cancelled()
            assert delivery.spontaneity_calls == [
                (10, "turn_source", "……等等，我这句还没说完。")
            ]
            assert len(store.list_deliveries_since(datetime(2026, 1, 1, tzinfo=UTC))) == 1
        finally:
            await router.aclose()
            _close(built)

    asyncio.run(scenario())


def test_new_user_row_invalidates_candidate_without_task_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        composer = FixedComposer()
        built = _build(tmp_path, composer)
        _, delivery, _, _, sessions, _, store, router = built
        source_user_message_id = _source_exchange(sessions)
        sessions.append_v2_exchange(
            10,
            "我又说了一句。",
            "收到。",
            turn_id="turn_newer",
            policy_act=ConversationAct.SHORT_ANSWER.value,
        )
        try:
            await router._run_spontaneity(
                chat_id=10,
                generation=sessions.current_generation(10),
                source_user_message_id=source_user_message_id,
                source_turn_id="turn_source",
                user_text="你确定没有其他边界了吗？",
                assistant_text="我现在还不能完全确定。",
                policy_act=ConversationAct.ADMIT_UNCERTAINTY,
                delay_seconds=0,
            )

            assert composer.calls == 0
            assert delivery.spontaneity_calls == []
            assert store.list_evaluations_since(datetime(2026, 1, 1, tzinfo=UTC)) == ()
        finally:
            await router.aclose()
            _close(built)

    asyncio.run(scenario())


def test_grounded_continuation_records_separate_short_horizon_stats(tmp_path: Path) -> None:
    async def scenario() -> None:
        composer = FixedComposer()
        built = _build(tmp_path, composer)
        _, delivery, _, _, sessions, _, store, router = built
        source_user_message_id = _source_exchange(sessions)
        try:
            await router._run_spontaneity(
                chat_id=10,
                generation=sessions.current_generation(10),
                source_user_message_id=source_user_message_id,
                source_turn_id="turn_source",
                user_text="你确定没有其他边界了吗？",
                assistant_text="我现在还不能完全确定。",
                policy_act=ConversationAct.ADMIT_UNCERTAINTY,
                delay_seconds=0,
            )

            assert composer.calls == 1
            assert delivery.spontaneity_calls == [
                (10, "turn_source", "……等等，我又想到一个边界条件。")
            ]
            evaluations = store.list_evaluations_since(datetime(2026, 1, 1, tzinfo=UTC))
            deliveries = store.list_deliveries_since(datetime(2026, 1, 1, tzinfo=UTC))
            assert len(evaluations) == 1
            assert evaluations[0].action is SpontaneityAction.CONTINUE
            assert len(deliveries) == 1
        finally:
            await router.aclose()
            _close(built)

    asyncio.run(scenario())
