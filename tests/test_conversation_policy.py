import asyncio
from pathlib import Path

from amadeus_bot.character import (
    POLICY_PROMPT_VERSION,
    AnswerObligation,
    ConversationAct,
    ConversationPolicyPlanner,
    PolicyContext,
    load_persona_core,
    render_policy_instruction,
)
from amadeus_bot.llm import LLMRequest, LLMResponse


class FixedProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="test-policy-model")


def _planner(text: str) -> tuple[ConversationPolicyPlanner, FixedProvider]:
    provider = FixedProvider(text)
    persona = load_persona_core(Path("profiles/v2/persona_core.json")).core
    return ConversationPolicyPlanner(provider=provider, persona=persona), provider


def test_valid_policy_is_parsed_and_prompt_is_versioned_for_persona_trigger() -> None:
    planner, provider = _planner(
        """{
          "act": "CHALLENGE",
          "intensity": 0.62,
          "answer_obligation": "partial",
          "memory_callback_ids": [],
          "state_bias": "mildly_prickly",
          "reason_label": "oversimplified_question"
        }"""
    )

    policy = asyncio.run(
        planner.plan(
            PolicyContext(
                user_message="大家都这么说，所以一定是真的吧？",
                recent_conversation=("user: previous", "assistant: previous reply"),
                recent_acts=(ConversationAct.SHORT_ANSWER,),
            )
        )
    )

    assert policy.act is ConversationAct.CHALLENGE
    assert policy.answer_obligation is AnswerObligation.PARTIAL
    assert policy.intensity == 0.62
    assert provider.requests[0].metadata["prompt_version"] == POLICY_PROMPT_VERSION
    assert "[CORE VALUES]" in provider.requests[0].messages[0].content
    assert "SHORT_ANSWER" in provider.requests[0].messages[1].content


def test_simple_social_turn_uses_local_fast_policy_without_provider_call() -> None:
    planner, provider = _planner("unused")

    policy = asyncio.run(planner.plan(PolicyContext(user_message="早上好")))

    assert policy.act is ConversationAct.SHORT_ANSWER
    assert policy.answer_obligation is AnswerObligation.MINIMAL
    assert policy.reason_label == "fast_simple_social"
    assert provider.requests == []


def test_neutral_explicit_task_uses_local_fast_policy_without_provider_call() -> None:
    planner, provider = _planner("unused")

    policy = asyncio.run(planner.plan(PolicyContext(user_message="解释一下神经网络")))

    assert policy.act is ConversationAct.DIRECT_ANSWER
    assert policy.answer_obligation is AnswerObligation.FULL
    assert policy.reason_label == "fast_explicit_task"
    assert provider.requests == []


def test_assistant_addressed_neutral_task_stays_fast() -> None:
    planner, provider = _planner("unused")

    policy = asyncio.run(planner.plan(PolicyContext(user_message="你能简单解释一下摩尔浓度吗？")))

    assert policy.act is ConversationAct.DIRECT_ANSWER
    assert policy.answer_obligation is AnswerObligation.FULL
    assert policy.reason_label == "fast_explicit_task"
    assert provider.requests == []


def test_low_stakes_emotion_uses_fast_emotional_policy() -> None:
    planner, provider = _planner("unused")

    policy = asyncio.run(planner.plan(PolicyContext(user_message="今天好累")))

    assert policy.act is ConversationAct.EMOTIONAL_RESPONSE
    assert policy.answer_obligation is AnswerObligation.MINIMAL
    assert policy.reason_label == "fast_low_stakes_emotion"
    assert provider.requests == []


def test_relationship_turn_keeps_llm_policy() -> None:
    planner, provider = _planner(
        '{"act":"DEFLECT","intensity":0.7,"answer_obligation":"partial",'
        '"memory_callback_ids":[],"state_bias":"embarrassed","reason_label":"relationship"}'
    )

    policy = asyncio.run(planner.plan(PolicyContext(user_message="你是不是喜欢我？")))

    assert policy.act is ConversationAct.DEFLECT
    assert len(provider.requests) == 1


def test_honesty_meta_turn_keeps_llm_policy() -> None:
    planner, provider = _planner(
        '{"act":"DEFLECT","intensity":0.6,"answer_obligation":"partial",'
        '"memory_callback_ids":[],"state_bias":"guarded","reason_label":"self_disclosure"}'
    )

    policy = asyncio.run(
        planner.plan(
            PolicyContext(
                user_message=(
                    "哇 没想到你会这么坦率，坦率得都不像你了。"
                    "会有什么问题我问了但你不会老实回答的么"
                )
            )
        )
    )

    assert policy.act is ConversationAct.DEFLECT
    assert len(provider.requests) == 1


def test_self_evaluation_turn_keeps_llm_policy() -> None:
    planner, provider = _planner(
        '{"act":"EMOTIONAL_RESPONSE","intensity":0.5,"answer_obligation":"partial",'
        '"memory_callback_ids":[],"state_bias":"reflective","reason_label":"self_evaluation"}'
    )

    policy = asyncio.run(
        planner.plan(
            PolicyContext(user_message="时间回退一下！按照同样的标准，你又会怎么评价你自己呢")
        )
    )

    assert policy.act is ConversationAct.EMOTIONAL_RESPONSE
    assert len(provider.requests) == 1


def test_persona_trigger_keeps_llm_policy() -> None:
    planner, provider = _planner(
        '{"act":"CHALLENGE","intensity":0.6,"answer_obligation":"minimal",'
        '"memory_callback_ids":[],"state_bias":"annoyed","reason_label":"nickname"}'
    )

    policy = asyncio.run(planner.plan(PolicyContext(user_message="Christina，早啊")))

    assert policy.act is ConversationAct.CHALLENGE
    assert len(provider.requests) == 1


def test_direct_user_message_cannot_be_silenced() -> None:
    planner, provider = _planner(
        '{"act":"SILENCE","intensity":0.2,"answer_obligation":"none",'
        '"memory_callback_ids":[],"state_bias":"","reason_label":"avoidance"}'
    )

    policy = asyncio.run(planner.plan(PolicyContext(user_message="你是不是不想理我？")))

    assert policy.act is ConversationAct.SHORT_ANSWER
    assert policy.answer_obligation is AnswerObligation.MINIMAL
    assert "direct_message_guard" in policy.reason_label
    assert len(provider.requests) == 1


def test_full_answer_guard_can_skip_redundant_policy_llm_for_neutral_task() -> None:
    planner, provider = _planner("unused")

    policy = asyncio.run(
        planner.plan(
            PolicyContext(
                user_message="这个服务器删除命令具体会删除哪些文件？",
                requires_full_answer=True,
            )
        )
    )

    assert policy.act is ConversationAct.DIRECT_ANSWER
    assert policy.answer_obligation is AnswerObligation.FULL
    assert "fast_full_answer" in policy.reason_label
    assert "full_answer_guard" in policy.reason_label
    assert provider.requests == []


def test_repetition_guard_breaks_repeated_teasing_on_delicate_turn() -> None:
    planner, provider = _planner(
        '{"act":"TEASE","intensity":0.5,"answer_obligation":"partial",'
        '"memory_callback_ids":[],"state_bias":"","reason_label":"nickname"}'
    )

    policy = asyncio.run(
        planner.plan(
            PolicyContext(
                user_message="又怎么了？",
                recent_acts=(
                    ConversationAct.TEASE,
                    ConversationAct.TEASE,
                    ConversationAct.TEASE,
                ),
            )
        )
    )

    assert policy.act is ConversationAct.SHORT_ANSWER
    assert policy.answer_obligation is AnswerObligation.PARTIAL
    assert "repetition_guard" in policy.reason_label
    assert len(provider.requests) == 1


def test_invalid_hidden_policy_falls_back_without_failing_sensitive_user_turn() -> None:
    planner, provider = _planner("this is not json")

    policy = asyncio.run(planner.plan(PolicyContext(user_message="你是不是在敷衍我？")))

    assert policy.act is ConversationAct.DIRECT_ANSWER
    assert policy.answer_obligation is AnswerObligation.FULL
    assert policy.reason_label == "policy_failure"
    assert len(provider.requests) == 1


def test_non_user_policy_failure_defaults_to_silence() -> None:
    planner, provider = _planner("not-json")

    policy = asyncio.run(
        planner.plan(
            PolicyContext(
                user_message="",
                message_type="autonomy",
            )
        )
    )

    assert policy.act is ConversationAct.SILENCE
    assert policy.answer_obligation is AnswerObligation.NONE
    assert len(provider.requests) == 1


def test_policy_instruction_is_user_invisible_control_text() -> None:
    planner, _ = _planner(
        '{"act":"DEFLECT","intensity":0.75,"answer_obligation":"none",'
        '"memory_callback_ids":[],"state_bias":"embarrassed_defensive",'
        '"reason_label":"relationship_probe"}'
    )
    policy = asyncio.run(planner.plan(PolicyContext(user_message="你是不是很喜欢我？")))

    rendered = render_policy_instruction(policy)

    assert "[CONVERSATION POLICY]" in rendered
    assert "act=DEFLECT" in rendered
    assert "answer_obligation=none" in rendered
    assert "Do not reveal" in rendered
