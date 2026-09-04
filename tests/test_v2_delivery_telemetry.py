import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.llm import LLMResponse
from amadeus_bot.runtime import SQLiteTurnTelemetryStore
from amadeus_bot.telegram import IncomingMessage, V2TelegramDeliveryAdapter


class FakeGateway:
    async def send_message(self, chat_id: int, text: str) -> int:
        return 9001

    async def send_typing(self, chat_id: int) -> None:
        return None


class FakeConversation:
    async def prepare_user_turn(self, chat_id: int, text: str, **kwargs: object) -> SimpleNamespace:
        timing = SimpleNamespace(
            policy_mode="fast",
            policy_ms=0,
            context_ms=2,
            generation_ms=4000,
            total_ms=4002,
        )
        policy = SimpleNamespace(
            act=SimpleNamespace(value="DIRECT_ANSWER"),
            reason_label="fast_explicit_task",
        )
        retrieval = SimpleNamespace(memory_ids=("mem_1",))
        response = LLMResponse(
            text="可以。",
            model="fake-model",
            usage={"input_tokens": 900, "output_tokens": 50, "total_tokens": 950},
        )
        return SimpleNamespace(
            turn_id="turn_delivery_telemetry",
            reply_text=response.text,
            retrieval_ms=3,
            retrieval=retrieval,
            turn_result=SimpleNamespace(timing=timing, policy=policy, response=response),
        )

    async def finalize_delivered_turn(
        self,
        prepared: object,
        **kwargs: object,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            exchange=SimpleNamespace(user_message_id=10, assistant_message_id=11),
            retrospective=None,
            warnings=(),
        )


class FakePreferences:
    def memory_enabled(self, chat_id: int) -> bool:
        return True

    def observe_user_text(self, chat_id: int, text: str, *, at: datetime) -> None:
        return None


class UnusedSessions:
    pass


class UnusedAutonomy:
    pass


class UnusedAutonomyGenerator:
    pass


def test_delivery_records_content_light_turn_telemetry(tmp_path: Path) -> None:
    async def scenario() -> None:
        telemetry = SQLiteTurnTelemetryStore(tmp_path / "turn-telemetry.sqlite")
        adapter = V2TelegramDeliveryAdapter(
            gateway=FakeGateway(),
            conversation=cast(Any, FakeConversation()),
            autonomy=cast(Any, UnusedAutonomy()),
            autonomy_message_generator=cast(Any, UnusedAutonomyGenerator()),
            sessions=cast(Any, UnusedSessions()),
            preferences=cast(Any, FakePreferences()),
            telemetry=telemetry,
        )
        try:
            incoming = IncomingMessage(
                chat_id=42,
                user_id=42,
                message_id=100,
                text="帮我解释一下摩尔浓度",
                received_at=datetime.now(UTC) - timedelta(milliseconds=100),
            )
            result = await adapter.deliver_user_message(incoming)
            assert result.warnings == ()

            record = telemetry.get("turn_delivery_telemetry")
            assert record is not None
            assert record.chat_id == 42
            assert record.user_message_id == 10
            assert record.assistant_message_id == 11
            assert record.telegram_message_id == 9001
            assert record.policy_mode == "fast"
            assert record.policy_reason_label == "fast_explicit_task"
            assert record.retrieved_memory_ids == ("mem_1",)
            assert record.memory_enabled is True
            assert record.input_tokens == 900
            assert record.output_tokens == 50
            assert record.total_tokens == 950
            assert record.queue_wait_ms >= 50
            assert record.generation_ms == 4000
        finally:
            telemetry.close()

    asyncio.run(scenario())
