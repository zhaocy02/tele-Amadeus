import asyncio
import json

import httpx

from amadeus_bot.llm import LLMImage, LLMMessage, LLMRequest, MessageRole, ResponsesAPIProvider


def test_responses_provider_serializes_ephemeral_image_data_url() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            current = payload["input"][0]
            assert current["role"] == "user"
            assert current["content"][0] == {"type": "input_text", "text": "look"}
            image = current["content"][1]
            assert image["type"] == "input_image"
            assert image["image_url"] == "data:image/png;base64,iVBORw0KGgo="
            return httpx.Response(
                200,
                json={
                    "model": "gpt-test",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "seen"}],
                        }
                    ],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ResponsesAPIProvider(
            base_url="http://provider.test",
            api_key=None,
            default_model="gpt-test",
            reasoning_effort="none",
            client=client,
        )
        response = await provider.generate(
            LLMRequest(
                messages=(
                    LLMMessage(
                        MessageRole.USER,
                        "look",
                        images=(LLMImage(data=b"\x89PNG\r\n\x1a\n", media_type="image/png"),),
                    ),
                )
            )
        )
        assert response.text == "seen"
        await client.aclose()

    asyncio.run(scenario())
