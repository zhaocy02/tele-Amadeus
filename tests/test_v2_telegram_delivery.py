import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from amadeus_bot.character import (
    AutonomyAction,
    AutonomyDecision,
    AutonomyOpportunity,
    AutonomySignal,
    AutonomySignalKind,
)
from amadeus_bot.llm import LLMResponse
from amadeus_bot.runtime import AutonomyRuntimeEvaluation
from amadeus_bot.telegram import IncomingMessage, V2TelegramDeliveryAdapter


@dataclass
class FakePrepared:
    reply_text: str = "v2 reply"


@dataclass
class FakeFinalize:
    exchange: object | None = object()
    warnings: tuple[str, ...] = ()


class FakeConversation:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.finalize_calls = 0

    def has_active_turn(self, chat_id: int) -> bool:
        return False

    async def prepare_user_turn(self, chat_id: int, text: str, **kwargs: object) -> FakePrepared:
        self.events.append("prepare")
        return FakePrepared()

    async def finalize_delivered_turn(self, prepared: object, **kwargs: object) -> FakeFinalize:
        self.events.append("finalize")
        self.finalize_calls += 1
        return FakeFinalize()


class FakeGateway:
    def __init__(self, events: list[str], *, fail_send: bool = False) -> None:
        self.events = events
        self.fail_send = fail_send

    async def send_message(self, chat_id: int, text: str) -> int:
        self.events.append("send")
        if self.fail_send:
            raise RuntimeError("telegram unavailable")
        return 9001

    async def send_typing(self, chat_id: int) -> None:
        return None


class FakeAutonomyGenerator:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def generate(self, **kwargs: object) -> LLMResponse:
        self.events.append("autonomy_generate")
        return LLMResponse(text="那个话题……还想继续的话，我可以听听。", model="fake")


class FakeSessions:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def history(self, chat_id: int, limit: int) -> tuple[object, ...]:
        return ()

    def append_v2_autonomy_message(self, *args: object, **kwargs: object) -> object:
        self.events.append("autonomy_transcript")
        return object()

    def append_v2_spontaneity_message(self, *args: object, **kwargs: object) -> object:
        self.events.append("spontaneity_transcript")
        return object()


class FakeAutonomy:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.external_records = 0

    def validate_delivery_candidate(self, evaluation: AutonomyRuntimeEvaluation) -> None:
        self.events.append("autonomy_preflight")

    def record_confirmed_external_delivery(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        **kwargs: object,
    ) -> object:
        self.events.append("autonomy_stats")
        self.external_records += 1
        return object()


class FakeSpontaneityStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.records = 0

    def record_delivery(self, *args: object, **kwargs: object) -> object:
        self.events.append("spontaneity_stats")
        self.records += 1
        return object()


def _incoming() -> IncomingMessage:
    return IncomingMessage(
        chat_id=42,
        user_id=42,
        message_id=100,
        text="测试 v2 delivery",
        received_at=datetime.now(UTC),
    )


def _evaluation() -> AutonomyRuntimeEvaluation:
    now = datetime.now(UTC)
    signal = AutonomySignal(
        signal_id="open-thread:thread_1",
        kind=AutonomySignalKind.OPEN_THREAD,
        summary="继续之前的话题",
        salience=0.9,
        source_thread_id="thread_1",
    )
    return AutonomyRuntimeEvaluation(
        chat_id=42,
        generation=1,
        evaluated_at=now - timedelta(seconds=1),
        due=True,
        opportunity=AutonomyOpportunity(
            now=now - timedelta(seconds=1),
            last_user_message_at=now - timedelta(hours=3),
            signals=(signal,),
        ),
        decision=AutonomyDecision(
            action=AutonomyAction.FOLLOW_UP,
            selected_signal_id=signal.signal_id,
            motivation=0.8,
            focus="继续之前的话题",
            reason_label="grounded",
        ),
    )


def _adapter(
    events: list[str],
    *,
    fail_send: bool = False,
) -> tuple[V2TelegramDeliveryAdapter, FakeConversation, FakeAutonomy, FakeSpontaneityStore]:
    conversation = FakeConversation(events)
    autonomy = FakeAutonomy(events)
    spontaneity = FakeSpontaneityStore(events)
    return (
        V2TelegramDeliveryAdapter(
            gateway=FakeGateway(events, fail_send=fail_send),  # type: ignore[arg-type]
            conversation=conversation,  # type: ignore[arg-type]
            autonomy=autonomy,  # type: ignore[arg-type]
            autonomy_message_generator=FakeAutonomyGenerator(events),  # type: ignore[arg-type]
            sessions=FakeSessions(events),  # type: ignore[arg-type]
            spontaneity_store=spontaneity,  # type: ignore[arg-type]
        ),
        conversation,
        autonomy,
        spontaneity,
    )


def test_user_turn_finalizes_only_after_telegram_send_success() -> None:
    async def scenario() -> None:
        events: list[str] = []
        adapter, conversation, _, _ = _adapter(events)

        result = await adapter.deliver_user_message(_incoming())

        assert result.telegram_message_id == 9001
        assert result.warnings == ()
        assert conversation.finalize_calls == 1
        assert events.index("send") < events.index("finalize")

    asyncio.run(scenario())


def test_user_turn_send_failure_never_finalizes_generated_reply() -> None:
    async def scenario() -> None:
        events: list[str] = []
        adapter, conversation, _, _ = _adapter(events, fail_send=True)

        with pytest.raises(RuntimeError, match="telegram unavailable"):
            await adapter.deliver_user_message(_incoming())

        assert conversation.finalize_calls == 0
        assert "finalize" not in events

    asyncio.run(scenario())


def test_autonomy_send_records_transcript_and_stats_only_after_transport_success() -> None:
    async def scenario() -> None:
        events: list[str] = []
        adapter, _, autonomy, _ = _adapter(events)

        result = await adapter.deliver_autonomy_evaluation(_evaluation())

        assert result.telegram_message_id == 9001
        assert result.warnings == ()
        assert autonomy.external_records == 1
        assert events.index("autonomy_generate") < events.index("send")
        assert events.index("send") < events.index("autonomy_transcript")
        assert events.index("send") < events.index("autonomy_stats")

    asyncio.run(scenario())


def test_autonomy_send_failure_does_not_record_delivery_or_transcript() -> None:
    async def scenario() -> None:
        events: list[str] = []
        adapter, _, autonomy, _ = _adapter(events, fail_send=True)

        with pytest.raises(RuntimeError, match="telegram unavailable"):
            await adapter.deliver_autonomy_evaluation(_evaluation())

        assert autonomy.external_records == 0
        assert "autonomy_transcript" not in events
        assert "autonomy_stats" not in events

    asyncio.run(scenario())


def test_spontaneity_send_persists_only_after_transport_success() -> None:
    async def scenario() -> None:
        events: list[str] = []
        adapter, _, _, spontaneity = _adapter(events)

        result = await adapter.deliver_spontaneity_message(
            chat_id=42,
            generation=1,
            source_turn_id="turn_source",
            text="……等等，我还有一点。",
            evaluation_id=7,
        )

        assert result.telegram_message_id == 9001
        assert result.warnings == ()
        assert spontaneity.records == 1
        assert events.index("send") < events.index("spontaneity_transcript")
        assert events.index("send") < events.index("spontaneity_stats")

    asyncio.run(scenario())


def test_spontaneity_send_failure_records_nothing() -> None:
    async def scenario() -> None:
        events: list[str] = []
        adapter, _, _, spontaneity = _adapter(events, fail_send=True)

        with pytest.raises(RuntimeError, match="telegram unavailable"):
            await adapter.deliver_spontaneity_message(
                chat_id=42,
                generation=1,
                source_turn_id="turn_source",
                text="这条不能持久化。",
                evaluation_id=7,
            )

        assert spontaneity.records == 0
        assert "spontaneity_transcript" not in events
        assert "spontaneity_stats" not in events

    asyncio.run(scenario())
