import asyncio

import httpx
import pytest

from amadeus_bot.tools import (
    ResponsesWebSearchProvider,
    WebSearchError,
    WebSearchProviderRegistry,
)


def _handler(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "search",
                        "queries": ["today"],
                    },
                },
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "open_page",
                        "url": "https://example.com/current",
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Current result.",
                            "annotations": [],
                        }
                    ],
                },
            ]
        },
    )


def _query_only_handler(request: httpx.Request) -> httpx.Response:
    del request
    return httpx.Response(
        200,
        json={
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "type": "search",
                        "queries": ["today"],
                    },
                },
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Ungrounded result.",
                            "annotations": [],
                        }
                    ],
                },
            ]
        },
    )


def test_responses_web_search_provider_collects_open_page_source_url() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        provider = ResponsesWebSearchProvider(
            provider_name="deepseek-native-web-search",
            provider_label="DeepSeek",
            base_url="https://api.deepseek.com",
            api_key="secret",
            model="deepseek-v4-pro",
            client=client,
        )
        try:
            evidence = await provider.search("today")
        finally:
            await client.aclose()

        assert evidence.provider == "deepseek-native-web-search"
        assert evidence.results[0].url == "https://example.com/current"

    asyncio.run(scenario())


def test_responses_web_search_provider_does_not_treat_query_as_source() -> None:
    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(_query_only_handler))
        provider = ResponsesWebSearchProvider(
            provider_name="deepseek-native-web-search",
            provider_label="DeepSeek",
            base_url="https://api.deepseek.com",
            api_key="secret",
            model="deepseek-v4-pro",
            client=client,
        )
        try:
            with pytest.raises(WebSearchError, match="without source evidence"):
                await provider.search("today")
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_web_search_registry_selection_is_independent() -> None:
    async def scenario() -> None:
        cpa_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        ds_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        cpa = ResponsesWebSearchProvider(
            provider_name="cpa-search",
            provider_label="CPA",
            base_url="http://127.0.0.1:8317",
            api_key="cpa",
            model="gpt",
            client=cpa_client,
        )
        deepseek = ResponsesWebSearchProvider(
            provider_name="deepseek-search",
            provider_label="DeepSeek",
            base_url="https://api.deepseek.com",
            api_key="ds",
            model="deepseek-v4-pro",
            client=ds_client,
        )
        registry = WebSearchProviderRegistry(
            {"cpa": cpa, "deepseek": deepseek},
            default_provider="cpa",
        )
        try:
            assert (await registry.search("today")).provider == "cpa-search"
            with registry.use("deepseek"):
                assert (await registry.search("today")).provider == "deepseek-search"
            assert registry.selected_provider == "cpa"
        finally:
            await registry.aclose()
            await cpa_client.aclose()
            await ds_client.aclose()

    asyncio.run(scenario())
