import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.llm import LLMRequest, LLMResponse, ProviderProfile, ProviderRegistry
from amadeus_bot.runtime import RuntimeProviderControl, SQLiteProviderPreferenceStore
from amadeus_bot.telegram import IncomingMessage, ProviderAwareV2TelegramDeliveryAdapter


class FakeLLM:
    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="ok", model=request.model or "fake")

    async def aclose(self) -> None:
        return None


class FakeGateway:
    async def send_message(self, chat_id: int, text: str) -> int:
        del chat_id, text
        return 1

    async def send_typing(self, chat_id: int) -> None:
        del chat_id

    async def download_image(self, attachment: object, *, max_bytes: int) -> object:
        del attachment, max_bytes
        raise AssertionError("no image expected")


class FakeConversation:
    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self.seen: list[str] = []

    async def prepare_user_turn(self, chat_id: int, text: str, **kwargs: object) -> object:
        del chat_id, text, kwargs
        self.seen.append(self.registry.selected_provider)
        response = LLMResponse(text="reply", model=self.registry.profile().text_model)
        timing = SimpleNamespace(
            policy_mode="fast",
            policy_ms=0,
            context_ms=0,
            generation_ms=1,
            total_ms=1,
        )
        turn_result = SimpleNamespace(
            response=response,
            timing=timing,
            policy=SimpleNamespace(act=SimpleNamespace(value="DIRECT_ANSWER")),
        )
        return SimpleNamespace(
            reply_text="reply",
            retrieval_ms=0,
            turn_result=turn_result,
            retrieval=SimpleNamespace(memory_ids=()),
            turn_id="turn_test",
            user_text="hello",
            user_images_count=0,
        )

    async def finalize_delivered_turn(self, prepared: object, **kwargs: object) -> object:
        del prepared, kwargs
        self.seen.append(self.registry.selected_provider)
        return SimpleNamespace(exchange=None, retrospective=None, warnings=())

    def has_active_turn(self, chat_id: int) -> bool:
        del chat_id
        return False


class FakeAutonomy:
    def validate_delivery_candidate(self, evaluation: object) -> None:
        del evaluation


class FakeAutonomyGenerator:
    async def generate(self, **kwargs: object) -> object:
        del kwargs
        return SimpleNamespace(text="autonomy")


class FakeSessions:
    def history(self, chat_id: int, limit: int) -> tuple[object, ...]:
        del chat_id, limit
        return ()


def test_direct_delivery_keeps_selected_provider_through_finalize(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = ProviderRegistry(
            (
                ProviderProfile(
                    name="cpa",
                    provider=FakeLLM(),
                    text_model="gpt",
                    vision_model="gpt",
                ),
                ProviderProfile(
                    name="deepseek",
                    provider=FakeLLM(),
                    text_model="deepseek-v4-pro",
                    vision_model="deepseek-v4-flash-vision-exp",
                ),
            ),
            default_provider="cpa",
        )
        provider_preferences = SQLiteProviderPreferenceStore(tmp_path / "providers.sqlite")
        provider_preferences.set_llm_provider(10, "deepseek")
        control = RuntimeProviderControl(
            llm_registry=registry,
            preferences=provider_preferences,
        )
        conversation = FakeConversation(registry)
        delivery = ProviderAwareV2TelegramDeliveryAdapter(
            provider_control=control,
            gateway=cast(Any, FakeGateway()),
            conversation=cast(Any, conversation),
            autonomy=cast(Any, FakeAutonomy()),
            autonomy_message_generator=cast(Any, FakeAutonomyGenerator()),
            sessions=cast(Any, FakeSessions()),
        )
        try:
            result = await delivery.deliver_user_message(
                IncomingMessage(
                    chat_id=10,
                    user_id=10,
                    message_id=1,
                    text="hello",
                    received_at=datetime.now(UTC),
                )
            )
            assert result.prepared.reply_text == "reply"
            assert conversation.seen == ["deepseek", "deepseek"]
            assert registry.selected_provider == "cpa"
        finally:
            provider_preferences.close()
            await registry.aclose()

    asyncio.run(scenario())
