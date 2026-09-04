from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from amadeus_bot.character.context import BuiltCharacterContext
from amadeus_bot.character.generator import CharacterGenerator
from amadeus_bot.llm import LLMMessage, MessageRole, ResponsesAPIProvider
from amadeus_bot.provider_capability_smoke import _config_from_environment, _web_provider
from amadeus_bot.tools.dispatcher import CharacterToolDispatcher, WebSearchStatus
from amadeus_bot.tools.web_search import (
    WebSearchError,
    WebSearchEvidence,
    WebSearchProvider,
    WebSearchResult,
)


class CharacterWebToolLoopSmokeError(RuntimeError):
    """Focused non-polling Character tool-loop smoke failure."""


class _WeakFirstWebProvider:
    def __init__(self, delegate: WebSearchProvider) -> None:
        self._delegate = delegate
        self._calls = 0

    @property
    def provider_name(self) -> str:
        return f"smoke-weak-first->{self._delegate.provider_name}"

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence:
        self._calls += 1
        if self._calls == 1:
            return WebSearchEvidence(
                query=query,
                retrieved_at=datetime.now(UTC),
                results=(
                    WebSearchResult(
                        title="SMOKE intentionally insufficient result",
                        url="https://smoke.invalid/insufficient",
                        snippet=(
                            "This fixture intentionally omits the requested official API detail. "
                            "A materially revised search is required."
                        ),
                    ),
                ),
                provider="smoke-weak-first",
                summary=(
                    "Insufficient evidence for the requested current API details. Revise the query "
                    "toward official or primary-source documentation and search again."
                ),
            )
        return await self._delegate.search(query, limit=limit)

    async def aclose(self) -> None:
        return None


class _FailingWebProvider:
    @property
    def provider_name(self) -> str:
        return "smoke-forced-failure"

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence:
        del query, limit
        raise WebSearchError("smoke forced provider failure")

    async def aclose(self) -> None:
        return None


def _llm_provider(name: str) -> ResponsesAPIProvider:
    config = _config_from_environment(name)
    return ResponsesAPIProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        default_model=config.text_model,
        reasoning_effort=config.reasoning_effort,
        timeout_seconds=config.timeout_seconds,
    )


def _context(
    *,
    current_user_message: str,
    developer_instruction: str,
    recent_conversation: tuple[LLMMessage, ...] = (),
) -> BuiltCharacterContext:
    return BuiltCharacterContext(
        messages=(
            LLMMessage(MessageRole.DEVELOPER, developer_instruction),
            *recent_conversation,
            LLMMessage(MessageRole.USER, current_user_message),
        ),
        prompt_version="character-web-tool-loop-smoke-v1",
        persona_version="smoke",
        persona_hash="smoke",
    )


def _brief(text: str, *, limit: int = 320) -> str:
    return " ".join(text.split())[:limit]


async def _contextual_cross_provider(*, llm_name: str, web_name: str) -> None:
    llm = _llm_provider(llm_name)
    web = _web_provider(_config_from_environment(web_name))
    dispatcher = CharacterToolDispatcher(web_search_provider=web)
    generator = CharacterGenerator(provider=llm)
    current = "把 MiniMax 的情况也一起搜了"
    context = _context(
        current_user_message=current,
        developer_instruction=(
            "This is a focused runtime validation turn, not a normal user session. The user is "
            "explicitly asking for web search. Use web_search. Resolve the contextual follow-up "
            "from the visible transcript and formulate a standalone query containing MiniMax and "
            "the realtime/end-to-end speech or voice API topic. After evidence, answer briefly."
        ),
        recent_conversation=(
            LLMMessage(
                MessageRole.USER,
                "我们在比较中国可以通过 API 使用的实时语音、语音端到端大模型，包括豆包和 Qwen。",
            ),
            LLMMessage(
                MessageRole.ASSISTANT,
                "可以，重点看实时语音对话、端到端能力、API 可用性和接入方式。",
            ),
        ),
    )
    try:
        result = await generator.generate_with_tools(
            context,
            tool_dispatcher=dispatcher,
            current_user_message=current,
        )
    finally:
        await llm.aclose()
        await web.aclose()

    if not result.web_search_outcomes:
        raise CharacterWebToolLoopSmokeError("contextual follow-up produced no web search")
    first = result.web_search_outcomes[0]
    folded = first.query.casefold()
    if first.status is not WebSearchStatus.SUCCESS:
        raise CharacterWebToolLoopSmokeError(
            f"contextual search did not succeed: {first.status.value}"
        )
    if "minimax" not in folded:
        raise CharacterWebToolLoopSmokeError("contextual query omitted MiniMax")
    topical_cues = ("语音", "speech", "voice", "audio", "端到端", "end-to-end", "api")
    if not any(cue in folded for cue in topical_cues):
        raise CharacterWebToolLoopSmokeError("contextual query omitted the prior speech/API topic")
    if first.provider != web.provider_name:
        raise CharacterWebToolLoopSmokeError(
            f"web provider mismatch: expected={web.provider_name} actual={first.provider}"
        )

    print(f"cross_llm={llm_name}")
    print(f"cross_web={web_name}")
    print(f"contextual_query={first.query}")
    print(f"contextual_searches={len(result.web_search_outcomes)}")
    print(f"contextual_reply={_brief(result.response.text)}")
    print("CONTEXTUAL_CROSS_PROVIDER=PASS")


async def _retry_after_weak_result(*, llm_name: str, web_name: str) -> None:
    llm = _llm_provider(llm_name)
    real_web = _web_provider(_config_from_environment(web_name))
    weak_web = _WeakFirstWebProvider(real_web)
    dispatcher = CharacterToolDispatcher(web_search_provider=weak_web)
    generator = CharacterGenerator(provider=llm)
    current = "搜索并确认 MiniMax 当前实时语音 API 的官方接入信息。"
    context = _context(
        current_user_message=current,
        developer_instruction=(
            "This is a focused retry validation. Use web_search. The first tool result is "
            "intentionally insufficient. Inspect it, then issue a second web_search with a "
            "materially revised query aimed at official or primary-source MiniMax realtime voice "
            "API documentation before producing the final answer."
        ),
    )
    try:
        result = await generator.generate_with_tools(
            context,
            tool_dispatcher=dispatcher,
            current_user_message=current,
        )
    finally:
        await llm.aclose()
        await real_web.aclose()

    if len(result.web_search_outcomes) < 2:
        raise CharacterWebToolLoopSmokeError("weak evidence did not trigger a second search")
    first, second = result.web_search_outcomes[:2]
    if first.query.casefold() == second.query.casefold():
        raise CharacterWebToolLoopSmokeError("retry reused the same query instead of revising it")
    if second.status is not WebSearchStatus.SUCCESS:
        raise CharacterWebToolLoopSmokeError(
            f"revised real search did not succeed: {second.status.value}"
        )
    if second.provider != real_web.provider_name:
        raise CharacterWebToolLoopSmokeError(
            f"retry provider mismatch: expected={real_web.provider_name} actual={second.provider}"
        )

    print(f"retry_llm={llm_name}")
    print(f"retry_web={web_name}")
    print(f"retry_query_1={first.query}")
    print(f"retry_query_2={second.query}")
    print(f"retry_reply={_brief(result.response.text)}")
    print("WEAK_RESULT_RETRY=PASS")


async def _failure_is_fail_soft(*, llm_name: str) -> None:
    llm = _llm_provider(llm_name)
    dispatcher = CharacterToolDispatcher(web_search_provider=_FailingWebProvider())
    generator = CharacterGenerator(provider=llm)
    current = "请联网确认一个当前事实；如果搜索失败就明确告诉我。"
    context = _context(
        current_user_message=current,
        developer_instruction=(
            "This is a focused fail-soft validation. Use web_search once. If the tool result "
            "reports failure, do not claim verification and include the exact Chinese phrase "
            "'无法验证' in the final reply."
        ),
    )
    try:
        result = await generator.generate_with_tools(
            context,
            tool_dispatcher=dispatcher,
            current_user_message=current,
        )
    finally:
        await llm.aclose()

    if not result.web_search_outcomes:
        raise CharacterWebToolLoopSmokeError("failure scenario produced no tool attempt")
    if not all(item.status is WebSearchStatus.FAILED for item in result.web_search_outcomes):
        raise CharacterWebToolLoopSmokeError(
            "forced failure scenario returned a non-failed outcome"
        )
    if "无法验证" not in result.response.text:
        raise CharacterWebToolLoopSmokeError(
            "final reply did not preserve fail-soft verification semantics"
        )

    print(f"failure_llm={llm_name}")
    print(f"failure_attempts={len(result.web_search_outcomes)}")
    print(f"failure_reply={_brief(result.response.text)}")
    print("FAILURE_FAIL_SOFT=PASS")


async def _run(scenario: str) -> int:
    if scenario in {"cross", "all"}:
        await _contextual_cross_provider(llm_name="cpa", web_name="deepseek")
        await _contextual_cross_provider(llm_name="deepseek", web_name="cpa")
    if scenario in {"retry", "all"}:
        await _retry_after_weak_result(llm_name="cpa", web_name="deepseek")
    if scenario in {"failure", "all"}:
        await _failure_is_fail_soft(llm_name="cpa")
    print("CHARACTER_WEB_TOOL_LOOP_SMOKE=PASS")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run focused non-polling Character Web Search tool-loop acceptance smokes."
    )
    parser.add_argument(
        "--scenario",
        choices=("all", "cross", "retry", "failure"),
        default="all",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(args.scenario)))
    except (RuntimeError, ValueError) as exc:
        print(f"CHARACTER_WEB_TOOL_LOOP_SMOKE=FAIL reason={exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
