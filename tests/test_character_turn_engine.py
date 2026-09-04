import asyncio
from pathlib import Path

from amadeus_bot.character import (
    CHARACTER_PROMPT_VERSION,
    POLICY_PROMPT_VERSION,
    AnswerObligation,
    CharacterContextBuilder,
    CharacterGenerator,
    CharacterTurnEngine,
    CharacterTurnInput,
    ConversationAct,
    ConversationPolicyPlanner,
    load_persona_core,
)
from amadeus_bot.llm import LLMMessage, LLMRequest, LLMResponse, MessageRole

PERSONA_PATH = Path("profiles/v2/persona_core.json")


class SequenceProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = self._responses[len(self.requests) - 1]
        return LLMResponse(text=text, model="test-model", request_id=f"r{len(self.requests)}")


def test_turn_engine_runs_llm_policy_for_relationship_turn() -> None:
    provider = SequenceProvider(
        [
            '{"act":"DEFLECT","intensity":0.75,"answer_obligation":"none",'
            '"memory_callback_ids":[],"state_bias":"embarrassed_defensive",'
            '"reason_label":"relationship_probe"}',
            "……你为什么非得问得这么直接啊。",
        ]
    )
    persona = load_persona_core(PERSONA_PATH)
    engine = CharacterTurnEngine(
        policy_planner=ConversationPolicyPlanner(provider=provider, persona=persona.core),
        context_builder=CharacterContextBuilder(persona),
        generator=CharacterGenerator(provider=provider),
    )

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="你是不是很喜欢我？",
                recent_conversation=(
                    LLMMessage(MessageRole.USER, "你会想我吗？"),
                    LLMMessage(MessageRole.ASSISTANT, "我会注意到你很久没出现。"),
                ),
                relationship_memories=("用户曾经认真问过‘想念’的含义。",),
            )
        )
    )

    assert len(provider.requests) == 2
    assert provider.requests[0].metadata["prompt_version"] == POLICY_PROMPT_VERSION
    assert provider.requests[1].metadata["prompt_version"] == CHARACTER_PROMPT_VERSION
    assert provider.requests[1].metadata["persona_version"] == "kurisu-v2.1.0"
    assert result.policy.act is ConversationAct.DEFLECT
    assert result.policy.answer_obligation is AnswerObligation.NONE
    assert result.response.text == "……你为什么非得问得这么直接啊。"
    assert result.timing.policy_mode == "llm"
    assert "reason_label" not in provider.requests[1].messages[0].content
    assert "relationship_probe" not in provider.requests[1].messages[0].content
    assert provider.requests[1].messages[-1].content.endswith("你是不是很喜欢我？")


def test_turn_engine_full_answer_uses_fast_policy_then_character_generation() -> None:
    provider = SequenceProvider(["这个命令会递归删除目标目录，所以不能拿生产路径试。"]) 
    persona = load_persona_core(PERSONA_PATH)
    engine = CharacterTurnEngine(
        policy_planner=ConversationPolicyPlanner(provider=provider, persona=persona.core),
        context_builder=CharacterContextBuilder(persona),
        generator=CharacterGenerator(provider=provider),
    )

    result = asyncio.run(
        engine.generate_turn(
            CharacterTurnInput(
                current_user_message="这个删除命令会做什么？",
                requires_full_answer=True,
            )
        )
    )

    assert result.policy.act is ConversationAct.DIRECT_ANSWER
    assert result.policy.answer_obligation is AnswerObligation.FULL
    assert result.timing.policy_mode == "fast"
    assert len(provider.requests) == 1
    assert provider.requests[0].metadata["prompt_version"] == CHARACTER_PROMPT_VERSION
    developer = provider.requests[0].messages[0].content
    assert "act=DIRECT_ANSWER" in developer
    assert "answer_obligation=full" in developer


def test_character_generator_strips_outer_whitespace_on_fast_social_turn() -> None:
    provider = SequenceProvider(["  早。  "])
    persona = load_persona_core(PERSONA_PATH)
    engine = CharacterTurnEngine(
        policy_planner=ConversationPolicyPlanner(provider=provider, persona=persona.core),
        context_builder=CharacterContextBuilder(persona),
        generator=CharacterGenerator(provider=provider),
    )

    result = asyncio.run(engine.generate_turn(CharacterTurnInput(current_user_message="早上好")))

    assert result.response.text == "早。"
    assert result.timing.policy_mode == "fast"
    assert len(provider.requests) == 1
    assert provider.requests[0].metadata["prompt_version"] == CHARACTER_PROMPT_VERSION
