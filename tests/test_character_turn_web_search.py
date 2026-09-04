import asyncio
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.character import (
    CharacterContextBuilder,
    CharacterGenerator,
    CharacterTurnEngine,
    CharacterTurnInput,
    ConversationPolicyPlanner,
    load_persona_core,
)
from amadeus_bot.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    MessageRole,
)
from amadeus_bot.tools import (
    CharacterToolDispatcher,
    WebSearchEvidence,
    WebSearchResult,
    WebSearchStatus,
)

PERSONA_PATH = Path("profiles/v2/persona_core.json")


class SequenceCharacterProvider:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._responses[len(self.requests) - 1]


class FakeSearchProvider:
    provider_name = "fake-web"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence:
        self.queries.append(query)
        return WebSearchEvidence(
            query=query,
            retrieved_at=datetime(2026, 9, 4, 6, 0, tzinfo=UTC),
            results=(
                WebSearchResult(
                    title=f"Result for {query}",
                    url=f"https://example.com/{len(self.queries)}",
                    snippet="Current source evidence.",
                ),
            ),
            provider=self.provider_name,
            summary=f"Grounded synthesis for {query}.",
        )

    async def aclose(self) -> None:
        return None


def _engine(
    provider: SequenceCharacterProvider,
    search_provider: FakeSearchProvider,
) -> CharacterTurnEngine:
    persona = load_persona_core(PERSONA_PATH)
    return CharacterTurnEngine(
        policy_planner=ConversationPolicyPlanner(provider=provider, persona=persona.core),
        context_builder=CharacterContextBuilder(persona),
        generator=CharacterGenerator(provider=provider),
        tool_dispatcher=CharacterToolDispatcher(web_search_provider=search_provider),
    )


def _tool_response(call_id: str, query: str, continuation: str) -> LLMResponse:
    return LLMResponse(
        text="",
        model="test-model",
        request_id=f"r-{call_id}",
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        tool_calls=(
            LLMToolCall(
                call_id=call_id,
                name="web_search",
                arguments={"query": query},
            ),
        ),
        continuation=continuation,
    )


def _final_response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        model="test-model",
        request_id="r-final",
        usage={"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
    )


def test_character_llm_resolves_contextual_followup_into_search_query() -> None:
    provider = SequenceCharacterProvider(
        [
            _tool_response(
                "call_1",
                "MiniMax realtime speech API official documentation",
                "continuation-1",
            ),
            _final_response("MiniMax 这一项我也查过了；这里是整合后的结论。"),
        ]
    )
    search = FakeSearchProvider()
    engine = _engine(provider, search)

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="把 MiniMax 的情况也一起搜了",
                recent_conversation=(
                    LLMMessage(
                        MessageRole.USER,
                        "我想比较国内实时语音端到端大模型 API。",
                    ),
                    LLMMessage(
                        MessageRole.ASSISTANT,
                        "前面已经提到了豆包和 Qwen。",
                    ),
                ),
                requires_full_answer=True,
            )
        )
    )

    assert search.queries == ["MiniMax realtime speech API official documentation"]
    assert len(provider.requests) == 2
    assert provider.requests[0].tools[0].name == "web_search"
    assert provider.requests[0].tool_results == ()
    assert provider.requests[1].continuation == "continuation-1"
    assert provider.requests[1].tool_results[0].call_id == "call_1"
    assert "https://example.com/1" in provider.requests[1].tool_results[0].output
    assert result.response.text.startswith("MiniMax")
    assert result.web_search_outcomes[0].status is WebSearchStatus.SUCCESS
    assert result.timing.llm_rounds == 2


def test_character_llm_can_inspect_result_and_search_again() -> None:
    provider = SequenceCharacterProvider(
        [
            _tool_response("call_1", "MiniMax realtime API", "continuation-1"),
            _tool_response(
                "call_2",
                "site:minimaxi.com realtime speech API documentation",
                "continuation-2",
            ),
            _final_response("第二次结果更可靠，我按官方资料整理。"),
        ]
    )
    search = FakeSearchProvider()
    engine = _engine(provider, search)

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="查一下 MiniMax 的实时语音 API，信息不够就继续找。",
                requires_full_answer=True,
            )
        )
    )

    assert search.queries == [
        "MiniMax realtime API",
        "site:minimaxi.com realtime speech API documentation",
    ]
    assert len(provider.requests) == 3
    assert provider.requests[1].tool_results[0].call_id == "call_1"
    assert provider.requests[2].tool_results[0].call_id == "call_2"
    assert [outcome.status for outcome in result.web_search_outcomes] == [
        WebSearchStatus.SUCCESS,
        WebSearchStatus.SUCCESS,
    ]
    assert result.timing.llm_rounds == 3


def test_llm_decision_replaces_legacy_lexical_auto_trigger() -> None:
    provider = SequenceCharacterProvider(
        [_final_response("这轮我不需要联网也能直接回答你的问题。")]
    )
    search = FakeSearchProvider()
    engine = _engine(provider, search)

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="OpenAI 最近有什么新消息？",
                requires_full_answer=True,
            )
        )
    )

    assert search.queries == []
    assert len(provider.requests) == 1
    assert provider.requests[0].tools[0].name == "web_search"
    assert result.web_search_outcomes == ()
    assert "不需要联网" in result.response.text


def test_explicit_user_optout_removes_web_search_tool_for_turn() -> None:
    provider = SequenceCharacterProvider([_final_response("好，那我只按已有上下文回答。")])
    search = FakeSearchProvider()
    engine = _engine(provider, search)

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="不要联网搜索，直接按你已有知识回答。",
                requires_full_answer=True,
            )
        )
    )

    assert search.queries == []
    assert provider.requests[0].tools == ()
    runtime_note = provider.requests[0].messages[-1].content
    assert "web_search=blocked_by_user_for_this_turn" in runtime_note
    assert result.response.text.startswith("好")


def test_web_search_budget_is_capped_at_three_calls() -> None:
    provider = SequenceCharacterProvider(
        [
            _tool_response("call_1", "query one", "continuation-1"),
            _tool_response("call_2", "query two", "continuation-2"),
            _tool_response("call_3", "query three", "continuation-3"),
            _final_response("三次搜索后给出当前最可靠的结论。"),
        ]
    )
    search = FakeSearchProvider()
    engine = _engine(provider, search)

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="仔细查，必要时换关键词继续搜索。",
                requires_full_answer=True,
            )
        )
    )

    assert search.queries == ["query one", "query two", "query three"]
    assert len(result.web_search_outcomes) == 3
    assert len(provider.requests) == 4
    assert provider.requests[2].tools
    assert provider.requests[3].tools == ()
    assert provider.requests[3].tool_results[0].call_id == "call_3"
    assert result.timing.llm_rounds == 4


def test_only_one_real_search_executes_per_llm_round() -> None:
    provider = SequenceCharacterProvider(
        [
            LLMResponse(
                text="",
                model="test-model",
                request_id="r-batch",
                tool_calls=(
                    LLMToolCall(
                        call_id="call_1",
                        name="web_search",
                        arguments={"query": "first query"},
                    ),
                    LLMToolCall(
                        call_id="call_2",
                        name="web_search",
                        arguments={"query": "second query"},
                    ),
                ),
                continuation="continuation-batch",
            ),
            _final_response("先看完第一组结果，再决定是否继续。"),
        ]
    )
    search = FakeSearchProvider()
    engine = _engine(provider, search)

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="帮我搜一下这个问题。",
                requires_full_answer=True,
            )
        )
    )

    assert search.queries == ["first query"]
    assert len(result.web_search_outcomes) == 1
    outputs = provider.requests[1].tool_results
    assert len(outputs) == 2
    assert outputs[0].call_id == "call_1"
    assert "status=success" in outputs[0].output
    assert outputs[1].call_id == "call_2"
    assert "reason=one_search_per_llm_round" in outputs[1].output
