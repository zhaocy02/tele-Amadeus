import asyncio
import json
from pathlib import Path

import httpx

from amadeus_bot.app import build_v2_provider_registry
from amadeus_bot.config import load_settings
from amadeus_bot.llm import LLMMessage, LLMRequest, MessageRole, ResponsesAPIProvider


def _env() -> dict[str, str]:
    return {
        "AMADEUS_ENVIRONMENT": "test",
        "AMADEUS_TELEGRAM_BOT_TOKEN": "test-token",
        "AMADEUS_ALLOWED_USER_IDS": "123",
        "AMADEUS_PROVIDER_BASE_URL": "http://127.0.0.1:8317/v1",
        "AMADEUS_PROVIDER_API_KEY": "cpa-key",
        "AMADEUS_DOUBAO_API_KEY": "doubao-key",
    }


def test_doubao_profile_is_registered_without_changing_cpa_default(tmp_path: Path) -> None:
    settings = load_settings(_env(), cwd=tmp_path)
    registry = build_v2_provider_registry(settings)

    try:
        assert registry.default_provider == "cpa"
        assert registry.available_providers == ("cpa", "doubao")
        profile = registry.profile("doubao")
        assert profile.label == "Doubao / Volcengine Ark"
        assert profile.text_model == "doubao-seed-evolving"
        assert profile.vision_model == "doubao-seed-evolving"
    finally:
        asyncio.run(registry.aclose())


def test_responses_provider_respects_ark_v3_base_url_and_neutral_reasoning() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v3/responses"
            assert request.headers["authorization"] == "Bearer doubao-key"
            payload = json.loads(request.content)
            assert payload == {
                "model": "doubao-seed-evolving",
                "input": [{"role": "user", "content": "hello"}],
                "store": False,
            }
            return httpx.Response(
                200,
                json={
                    "id": "resp_doubao_test",
                    "model": "doubao-seed-evolving",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "hi"}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ResponsesAPIProvider(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key="doubao-key",
            default_model="doubao-seed-evolving",
            reasoning_effort="none",
            client=client,
        )
        try:
            response = await provider.generate(
                LLMRequest(messages=(LLMMessage(MessageRole.USER, "hello"),))
            )
            assert response.text == "hi"
            assert response.model == "doubao-seed-evolving"
        finally:
            await client.aclose()

    asyncio.run(scenario())
