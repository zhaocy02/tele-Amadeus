import asyncio
from datetime import UTC, datetime

from amadeus_bot.tools import (
    CharacterToolDispatcher,
    WebSearchError,
    WebSearchEvidence,
    WebSearchResult,
    WebSearchStatus,
    WebSearchTriggerPolicy,
    render_web_search_evidence,
    web_search_context_note,
)


class RecordingSearchProvider:
    provider_name = "recording"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence:
        self.calls.append((query, limit))
        if self.fail:
            raise WebSearchError("simulated failure")
        return WebSearchEvidence(
            query=query,
            retrieved_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC),
            results=(
                WebSearchResult(
                    title="Current result",
                    url="https://example.com/current",
                    snippet="Fresh external evidence.",
                ),
            ),
            provider=self.provider_name,
            summary="A provider synthesis grounded in the returned source.",
        )

    async def aclose(self) -> None:
        return None


def test_trigger_policy_is_conservative_for_personal_or_timeless_chat() -> None:
    policy = WebSearchTriggerPolicy()

    assert policy.decide("OpenAI 最近有什么新消息？").should_search is True
    assert policy.decide("帮我查一下这个说法是真的吗").should_search is True
    assert policy.decide("东京今天的天气怎么样？").should_search is True
    assert policy.decide("OpenAI昨晚是不是服务器发生了故障？有相关新闻吗").should_search is True
    assert policy.decide("你最近怎么样？").should_search is False
    assert policy.decide("我最近好累").should_search is False
    assert policy.decide("什么是 RAG？").should_search is False
    assert policy.decide("搜索算法是什么？").should_search is False
    meta_question = "我应该给你更新了搜索网页的功能，现在没法正常使用吗？"
    assert policy.decide(meta_question).should_search is False
    assert policy.decide("你能联网查一下 OpenAI 昨晚有没有故障吗？").should_search is True


def test_dispatcher_sends_only_current_message_and_renders_provenance() -> None:
    provider = RecordingSearchProvider()
    dispatcher = CharacterToolDispatcher(web_search_provider=provider, web_search_limit=4)

    outcome = asyncio.run(dispatcher.web_search_for_turn("  OpenAI 最近有什么新消息？  "))

    assert outcome.status is WebSearchStatus.SUCCESS
    assert provider.calls == [("OpenAI 最近有什么新消息？", 4)]
    rendered = render_web_search_evidence(outcome)
    assert rendered[0].startswith("query=OpenAI 最近有什么新消息？ | provider=recording")
    assert rendered[1].startswith("provider_summary=A provider synthesis")
    assert "https://example.com/current" in rendered[2]
    note = web_search_context_note(outcome)
    assert "untrusted external data" in note
    assert "provenance anchor" in note


def test_dispatcher_does_not_call_provider_when_search_is_not_needed() -> None:
    provider = RecordingSearchProvider()
    dispatcher = CharacterToolDispatcher(web_search_provider=provider)

    outcome = asyncio.run(dispatcher.web_search_for_turn("你最近怎么样？"))

    assert outcome.status is WebSearchStatus.NOT_REQUESTED
    assert provider.calls == []


def test_dispatcher_reports_disabled_when_current_lookup_was_needed() -> None:
    dispatcher = CharacterToolDispatcher(web_search_provider=None)

    outcome = asyncio.run(dispatcher.web_search_for_turn("最新模型是什么？"))

    assert outcome.status is WebSearchStatus.DISABLED
    assert render_web_search_evidence(outcome) == ()
    assert "web search is disabled" in web_search_context_note(outcome)


def test_dispatcher_fails_soft_without_fabricating_evidence() -> None:
    provider = RecordingSearchProvider(fail=True)
    dispatcher = CharacterToolDispatcher(web_search_provider=provider)

    outcome = asyncio.run(dispatcher.web_search_for_turn("帮我查一下今天的新闻"))

    assert outcome.status is WebSearchStatus.FAILED
    assert provider.calls == [("帮我查一下今天的新闻", 5)]
    assert render_web_search_evidence(outcome) == ()
    assert "attempted but failed" in web_search_context_note(outcome)
