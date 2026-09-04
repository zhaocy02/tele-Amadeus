import asyncio
import json
from datetime import UTC, datetime

import httpx

from amadeus_bot.tools import CPAWebSearchProvider, WebSearchError, normalize_web_search_query


def test_query_normalization_is_bounded() -> None:
    words = [f"word{i}" for i in range(80)]
    normalized = normalize_web_search_query("  " + "   ".join(words) + "  ")

    assert len(normalized) <= 400
    assert len(normalized.split()) <= 50


def test_cpa_web_search_sends_only_explicit_query_and_parses_sources() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_test",
                        "status": "completed",
                        "action": {
                            "type": "search",
                            "query": "OpenAI current model",
                            "sources": [
                                {
                                    "title": "OpenAI Models",
                                    "url": "https://openai.com/models",
                                }
                            ],
                        },
                        "results": [
                            {
                                "title": "OpenAI Models",
                                "url": "https://openai.com/models",
                                "description": "Current model information.",
                            },
                            {
                                "title": "Example",
                                "url": "https://example.com/story",
                                "snippet": "A second result.",
                            },
                        ],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "A concise current synthesis.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "OpenAI Models",
                                        "url": "https://openai.com/models",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CPAWebSearchProvider(
        base_url="http://127.0.0.1:8317/v1",
        api_key="secret-provider-key",
        model="gpt-test",
        client=client,
        clock=lambda: datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
    )
    try:
        evidence = asyncio.run(provider.search(" OpenAI   current model ", limit=5))
    finally:
        asyncio.run(client.aclose())

    assert len(seen) == 1
    request = seen[0]
    assert request.url == httpx.URL("http://127.0.0.1:8317/v1/responses")
    assert request.headers["Authorization"] == "Bearer secret-provider-key"
    payload = json.loads(request.content)
    assert payload["model"] == "gpt-test"
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "required"
    assert payload["store"] is False
    wire_text = payload["input"][0]["content"]
    assert "OpenAI current model" in wire_text
    assert "current local date/time is 2026-09-03T10:00:00+00:00" in wire_text
    assert "今天" in wire_text
    assert "memory" not in wire_text.lower()
    assert "transcript" not in wire_text.lower()

    assert evidence.provider == "cpa-native-web-search"
    assert evidence.query == "OpenAI current model"
    assert evidence.summary == "A concise current synthesis."
    assert [result.url for result in evidence.results] == [
        "https://openai.com/models",
        "https://example.com/story",
    ]
    assert "secret-provider-key" not in repr(evidence)


def test_cpa_web_search_accepts_citation_only_sources() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_test",
                        "status": "completed",
                        "action": {"type": "search", "query": "today"},
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Current information.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "Source",
                                        "url": "https://example.org/current",
                                    }
                                ],
                            }
                        ],
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CPAWebSearchProvider(
        base_url="http://127.0.0.1:8317",
        api_key=None,
        model="gpt-test",
        client=client,
    )
    try:
        evidence = asyncio.run(provider.search("today"))
    finally:
        asyncio.run(client.aclose())

    assert len(evidence.results) == 1
    assert evidence.results[0].url == "https://example.org/current"


def test_cpa_web_search_requires_actual_tool_execution() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "I did not search."}],
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CPAWebSearchProvider(
        base_url="http://127.0.0.1:8317",
        api_key=None,
        model="gpt-test",
        client=client,
    )
    try:
        try:
            asyncio.run(provider.search("today"))
        except WebSearchError as exc:
            assert str(exc) == "CPA response did not execute the web_search tool"
        else:
            raise AssertionError("expected WebSearchError")
    finally:
        asyncio.run(client.aclose())


def test_cpa_web_search_sanitizes_provider_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, text="sensitive upstream body")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = CPAWebSearchProvider(
        base_url="http://127.0.0.1:8317",
        api_key="secret",
        model="gpt-test",
        client=client,
    )
    try:
        try:
            asyncio.run(provider.search("today"))
        except WebSearchError as exc:
            assert str(exc) == "CPA web search returned HTTP 400"
            assert "sensitive" not in str(exc)
            assert "secret" not in str(exc)
        else:
            raise AssertionError("expected WebSearchError")
    finally:
        asyncio.run(client.aclose())
