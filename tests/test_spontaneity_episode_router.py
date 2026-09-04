import asyncio
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
from amadeus_bot.telegram import V2TelegramMessageRouter


class FakeGateway:
    async def send_message(self, chat_id: int, text: str) -> int:
        return 1

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


class EpisodeGate:
    def followup_allowed(self, turn_id: str, sequence_index: int) -> bool:
        return sequence_index <= 3

    def delay_for_followup(self, turn_id: str, sequence_index: int) -> float:
        return 0.0

    def interrupt_window_allowed(self, turn_id: str) -> bool:
        return False


class EpisodeComposer:
    def __init__(self) -> None:
        self.config = SpontaneityConfig(max_followups_per_episode=3)
        self.sequences: list[int] = []

    async def compose(self, opportunity: Any) -> SpontaneityDecision:
        self.sequences.append(opportunity.sequence_index)
        if opportunity.sequence_index == 3:
            return SpontaneityDecision(
                action=SpontaneityAction.SILENT,
                reason_label="thought_finished",
            )
        return SpontaneityDecision(
            action=SpontaneityAction.CONTINUE,
            motivation=0.8,
            focus=f"thought-{opportunity.sequence_index}",
            reason_label="afterthought",
            text=f"follow-up-{opportunity.sequence_index}",
        )


class FakeDelivery:
    def __init__(self, store: SQLiteSpontaneityStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str]] = []

    async def deliver_spontaneity_message(
        self,
        *,
        chat_id: int,
        generation: int,
        source_turn_id: str,
        text: str,
        evaluation_id: int | None,
    ) -> SimpleNamespace:
        self.calls.append((source_turn_id, text))
        stored = self.store.record_delivery(
            chat_id,
            generation,
            source_turn_id=source_turn_id,
            evaluation_id=evaluation_id,
            at=datetime.now(UTC),
        )
        return SimpleNamespace(
            telegram_message_id=9000 + len(self.calls),
            warnings=(),
            stored_delivery=stored,
        )


def test_episode_can_deliver_multiple_messages_then_stop_silent(tmp_path: Path) -> None:
    async def scenario() -> None:
        memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
        delivery = FakeDelivery(store)
        composer = EpisodeComposer()
        conversation = FakeConversation(sessions)
        router = V2TelegramMessageRouter(
            gateway=cast(Any, FakeGateway()),
            delivery=cast(Any, delivery),
            conversation=cast(Any, conversation),
            memory=memory,
            state=state,
            sessions=sessions,
            preferences=preferences,
            spontaneity_composer=cast(Any, composer),
            spontaneity_gate=cast(Any, EpisodeGate()),
            spontaneity_store=store,
            spontaneity_process_enabled=True,
        )
        preferences.set_autonomy_enabled(10, True)
        preferences.set_spontaneity_enabled(10, True)
        exchange = sessions.append_v2_exchange(
            10,
            "你还有什么想补充的吗？",
            "暂时就这些。",
            turn_id="turn_episode",
            policy_act=ConversationAct.SHORT_ANSWER.value,
        )
        try:
            await router._run_spontaneity_episode(
                chat_id=10,
                generation=sessions.current_generation(10),
                source_user_message_id=exchange.user_message_id,
                source_turn_id="turn_episode",
                user_text="你还有什么想补充的吗？",
                assistant_text="暂时就这些。",
                policy_act=ConversationAct.SHORT_ANSWER,
                allow_first_interrupt_window=False,
            )

            assert composer.sequences == [1, 2, 3]
            assert delivery.calls == [
                ("turn_episode", "follow-up-1"),
                ("turn_episode::spont:2", "follow-up-2"),
            ]
            episode = store.list_episode_messages("turn_episode")
            assert [item.sequence_index for item in episode] == [1, 2]
            evaluations = store.list_evaluations_since(datetime(2026, 1, 1, tzinfo=UTC))
            assert len(evaluations) == 3
            assert evaluations[0].action is SpontaneityAction.SILENT
        finally:
            await router.aclose()
            store.close()
            preferences.close()
            sessions.close()
            state.close()
            memory.close()

    asyncio.run(scenario())
