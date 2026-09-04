import asyncio
import json

import httpx
import pytest

from amadeus_bot.vision_smoke import VisionCapabilityError, VisionCapabilityProbe


def test_vision_probe_sends_responses_input_image_and_verifies_red() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/responses"
            assert request.headers["authorization"] == "Bearer test-key"
            payload = json.loads(request.content)
            assert payload["model"] == "gpt-test"
            assert payload["store"] is False
            assert "reasoning" not in payload

            content = payload["input"][0]["content"]
            assert content[0]["type"] == "input_text"
            assert content[1]["type"] == "input_image"
            assert content[1]["image_url"].startswith("data:image/png;base64,")
            return httpx.Response(
                200,
                json={
                    "id": "resp_vision",
                    "model": "gpt-test",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "RED"}],
                        }
                    ],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        probe = VisionCapabilityProbe(
            base_url="http://provider.test/v1",
            api_key="test-key",
            model="gpt-test",
            client=client,
        )
        result = await probe.run()
        assert result.passed is True
        assert result.response_text == "RED"
        assert result.request_id == "resp_vision"
        await client.aclose()

    asyncio.run(scenario())


def test_vision_probe_surfaces_only_sanitized_error_identity() -> None:
    async def scenario() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "code": "unsupported_image_input",
                        "message": "secret internal diagnostic",
                    }
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        probe = VisionCapabilityProbe(
            base_url="http://provider.test",
            api_key="secret-key",
            model="gpt-test",
            client=client,
        )
        with pytest.raises(VisionCapabilityError) as captured:
            await probe.run()
        message = str(captured.value)
        assert "HTTP 400" in message
        assert "invalid_request_error" in message
        assert "unsupported_image_input" in message
        assert "secret internal diagnostic" not in message
        assert "secret-key" not in message
        await client.aclose()

    asyncio.run(scenario())


def test_vision_probe_marks_non_red_output_inconclusive() -> None:
    async def scenario() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "gpt-test",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "NOT_RED"}],
                        }
                    ],
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        probe = VisionCapabilityProbe(
            base_url="http://provider.test",
            api_key=None,
            model="gpt-test",
            client=client,
        )
        result = await probe.run()
        assert result.passed is False
        await client.aclose()

    asyncio.run(scenario())
