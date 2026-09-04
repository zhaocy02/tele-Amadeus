import asyncio
import json

import httpx
import pytest

from amadeus_bot.llm import (
    LLMMessage,
    LLMRequest,
    LLMToolDefinition,
    LLMToolResult,
    MessageRole,
    ProviderError,
    ResponsesAPIProvider,
)


def test_responses_provider_maps_domain_request_to_wire_api() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/responses"
            assert request.headers["authorization"] == "Bearer test-key"
            payload = json.loads(request.content)
            assert payload["model"] == "gpt-test"
            assert payload["instructions"] == "persona\n\nmemory"
            assert payload["input"] == [
                {"role": "user", "content": "hello"},
            ]
            assert payload["reasoning"] == {"effort": "high"}
            assert payload["store"] is False
            assert "metadata" not in payload
            assert "tools" not in payload
            return httpx.Response(
                200,
                json={
                    "id": "resp_123",
                    "model": "gpt-test",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "hi"}],
                        }
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ResponsesAPIProvider(
            base_url="http://provider.test/v1",
            api_key="test-key",
            default_model="fallback",
            client=client,
        )
        response = await provider.generate(
            LLMRequest(
                messages=(
                    LLMMessage(MessageRole.DEVELOPER, "persona"),
                    LLMMessage(MessageRole.SYSTEM, "memory"),
                    LLMMessage(MessageRole.USER, "hello"),
                ),
                model="gpt-test",
                metadata={"prompt_version": "test-only-trace"},
            )
        )
        assert response.text == "hi"
        assert response.request_id == "resp_123"
        assert response.usage["total_tokens"] == 7
        assert response.tool_calls == ()
        await client.aclose()

    asyncio.run(scenario())


def test_responses_provider_round_trips_function_call_continuation() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            payload = json.loads(request.content)
            assert payload["tools"] == [
                {
                    "type": "function",
                    "name": "web_search",
                    "description": "Search the public web.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ]
            assert payload["tool_choice"] == "auto"
            if calls == 1:
                assert payload["input"] == [{"role": "user", "content": "check Tokyo"}]
                return httpx.Response(
                    200,
                    json={
                        "id": "resp_tool_1",
                        "model": "gpt-test",
                        "output": [
                            {
                                "id": "rs_1",
                                "type": "reasoning",
                                "summary": [],
                            },
                            {
                                "id": "fc_1",
                                "type": "function_call",
                                "call_id": "call_1",
                                "name": "web_search",
                                "arguments": '{"query":"Tokyo weather today"}',
                                "status": "completed",
                            },
                        ],
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 4,
                            "total_tokens": 14,
                        },
                    },
                )

            assert calls == 2
            assert payload["input"] == [
                {"role": "user", "content": "check Tokyo"},
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [],
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "web_search",
                    "arguments": '{"query":"Tokyo weather today"}',
                    "status": "completed",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "status=success",
                },
            ]
            return httpx.Response(
                200,
                json={
                    "id": "resp_tool_2",
                    "model": "gpt-test",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Tokyo is clear today.",
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 20, "output_tokens": 6, "total_tokens": 26},
                },
            )

        tool = LLMToolDefinition(
            name="web_search",
            description="Search the public web.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ResponsesAPIProvider(
            base_url="http://provider.test/v1",
            api_key="test-key",
            default_model="gpt-test",
            client=client,
        )
        first = await provider.generate(
            LLMRequest(
                messages=(LLMMessage(MessageRole.USER, "check Tokyo"),),
                tools=(tool,),
            )
        )
        assert first.text == ""
        assert len(first.tool_calls) == 1
        assert first.tool_calls[0].name == "web_search"
        assert first.tool_calls[0].arguments == {"query": "Tokyo weather today"}
        assert first.continuation is not None

        second = await provider.generate(
            LLMRequest(
                messages=(LLMMessage(MessageRole.USER, "check Tokyo"),),
                tools=(tool,),
                tool_results=(LLMToolResult(call_id="call_1", output="status=success"),),
                continuation=first.continuation,
            )
        )
        assert second.text == "Tokyo is clear today."
        assert second.tool_calls == ()
        assert calls == 2
        await client.aclose()

    asyncio.run(scenario())


def test_responses_provider_surfaces_sanitized_http_failure() -> None:
    async def scenario() -> None:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="secret diagnostic body")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ResponsesAPIProvider(
            base_url="http://provider.test",
            api_key="secret-key",
            default_model="gpt-test",
            client=client,
        )
        with pytest.raises(ProviderError, match="HTTP 401") as captured:
            await provider.generate(
                LLMRequest(messages=(LLMMessage(MessageRole.USER, "hello"),))
            )
        assert "secret diagnostic body" not in str(captured.value)
        assert "secret-key" not in str(captured.value)
        await client.aclose()

    asyncio.run(scenario())
