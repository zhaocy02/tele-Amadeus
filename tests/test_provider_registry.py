import asyncio

from amadeus_bot.llm import (
    LLMImage,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ProviderProfile,
    ProviderRegistry,
)


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        await asyncio.sleep(0)
        assert request.model is not None
        return LLMResponse(text=self.name, model=request.model)

    async def aclose(self) -> None:
        self.closed = True


def _request(*, image: bool = False) -> LLMRequest:
    images = (LLMImage(b"image"),) if image else ()
    return LLMRequest(
        messages=(
            LLMMessage(
                role=MessageRole.USER,
                content="hello",
                images=images,
            ),
        ),
        model="caller-model-must-be-overridden",
    )


def test_provider_registry_routes_text_and_vision_models() -> None:
    async def scenario() -> None:
        cpa = FakeProvider("cpa")
        deepseek = FakeProvider("deepseek")
        registry = ProviderRegistry(
            (
                ProviderProfile(
                    name="cpa",
                    provider=cpa,
                    text_model="gpt-text",
                    vision_model="gpt-vision",
                ),
                ProviderProfile(
                    name="deepseek",
                    provider=deepseek,
                    text_model="deepseek-v4-pro",
                    vision_model="deepseek-v4-flash-vision-exp",
                ),
            ),
            default_provider="cpa",
        )
        try:
            response = await registry.generate(_request())
            assert response.text == "cpa"
            assert cpa.requests[-1].model == "gpt-text"

            with registry.use("deepseek"):
                text_response = await registry.generate(_request())
                image_response = await registry.generate(_request(image=True))

            assert text_response.model == "deepseek-v4-pro"
            assert image_response.model == "deepseek-v4-flash-vision-exp"
            assert deepseek.requests[-2].model == "deepseek-v4-pro"
            assert deepseek.requests[-1].model == "deepseek-v4-flash-vision-exp"
            assert registry.selected_provider == "cpa"
        finally:
            await registry.aclose()

        assert cpa.closed is True
        assert deepseek.closed is True

    asyncio.run(scenario())


def test_provider_registry_context_is_isolated_between_async_tasks() -> None:
    async def scenario() -> None:
        cpa = FakeProvider("cpa")
        deepseek = FakeProvider("deepseek")
        registry = ProviderRegistry(
            (
                ProviderProfile(name="cpa", provider=cpa, text_model="cpa-model"),
                ProviderProfile(
                    name="deepseek",
                    provider=deepseek,
                    text_model="deepseek-model",
                ),
            ),
            default_provider="cpa",
        )

        async def call(provider: str) -> tuple[str, str]:
            with registry.use(provider):
                await asyncio.sleep(0)
                response = await registry.generate(_request())
                return provider, response.model

        try:
            results = await asyncio.gather(call("cpa"), call("deepseek"))
        finally:
            await registry.aclose()

        assert results == [
            ("cpa", "cpa-model"),
            ("deepseek", "deepseek-model"),
        ]

    asyncio.run(scenario())
