import asyncio
from pathlib import Path

from amadeus_bot.character import (
    CHARACTER_PROMPT_VERSION,
    CharacterContextBuilder,
    CharacterGenerator,
    CharacterTurnEngine,
    CharacterTurnInput,
    ConversationPolicyPlanner,
    load_persona_core,
)
from amadeus_bot.character.canon import CanonExample, CanonRetriever
from amadeus_bot.llm import LLMRequest, LLMResponse

PERSONA_PATH = Path("profiles/v2/persona_core.json")


class CharacterOnlyProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        assert request.metadata.get("prompt_version") == CHARACTER_PROMPT_VERSION
        return LLMResponse(text="先把数据补齐。没有证据就别急着下结论。", model="test")


def test_turn_engine_injects_relevant_localized_canon_behavior_summary() -> None:
    provider = CharacterOnlyProvider()
    persona = load_persona_core(PERSONA_PATH)
    canon = CanonRetriever(
        (
            CanonExample(
                example_id="sg:science:1",
                source="sg",
                persona="kurisu",
                history=("Okabe: I think this proves it.",),
                response=(
                    "Kurisu raw English response that should not be injected "
                    "when summary exists."
                ),
                act="DIRECT_ANSWER",
                tags=("实验", "证据", "结论"),
                search_summary="面对实验数据不足却急着下结论时，先要求证据并直接纠正过度推断。",
            ),
            CanonExample(
                example_id="sg:banter:1",
                source="sg",
                persona="kurisu",
                history=(),
                response="Unrelated teasing response.",
                act="TEASE",
                tags=("调侃", "害羞"),
                search_summary="被亲近的人调侃害羞时先否认并反击。",
            ),
        ),
        min_score=0.05,
    )
    engine = CharacterTurnEngine(
        policy_planner=ConversationPolicyPlanner(provider=provider, persona=persona.core),
        context_builder=CharacterContextBuilder(persona),
        generator=CharacterGenerator(provider=provider),
        canon_retriever=canon,
    )

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(current_user_message="这个实验还没有数据，可以直接下结论吗？")
        )
    )

    assert result.timing.policy_mode == "fast"
    assert len(provider.requests) == 1
    developer = provider.requests[0].messages[0].content
    assert "[CANON BEHAVIOR EXAMPLES — REFERENCE, NOT CURRENT FACTS]" in developer
    assert "面对实验数据不足却急着下结论时" in developer
    assert "Kurisu raw English response" not in developer
    assert "被亲近的人调侃害羞时" not in developer


def test_turn_engine_without_canon_retriever_preserves_empty_boundary() -> None:
    provider = CharacterOnlyProvider()
    persona = load_persona_core(PERSONA_PATH)
    engine = CharacterTurnEngine(
        policy_planner=ConversationPolicyPlanner(provider=provider, persona=persona.core),
        context_builder=CharacterContextBuilder(persona),
        generator=CharacterGenerator(provider=provider),
    )

    asyncio.run(engine.generate_turn(CharacterTurnInput(current_user_message="早上好")))

    developer = provider.requests[0].messages[0].content
    assert "No canon behavior examples are supplied for this turn." in developer
