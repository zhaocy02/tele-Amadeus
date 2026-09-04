import asyncio
from datetime import UTC, datetime, timedelta
from math import isinf
from pathlib import Path

from amadeus_bot.character import (
    SPONTANEITY_PROMPT_VERSION,
    ConversationAct,
    SpontaneityAction,
    SpontaneityComposer,
    SpontaneityConfig,
    SpontaneityOpportunity,
    SpontaneityOpportunityGate,
    load_persona_core,
)
from amadeus_bot.llm import LLMMessage, LLMRequest, LLMResponse, MessageRole


class FixedProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="fake-spontaneity")


def _opportunity(sequence_index: int = 1) -> SpontaneityOpportunity:
    return SpontaneityOpportunity(
        source_turn_id="turn_spontaneity_1",
        user_text="你刚才说这个设计没问题，但真的没有其他漏洞了吗？",
        assistant_text="整体方向没问题，不过我还不敢说所有边界都已经处理干净。",
        policy_act=ConversationAct.ADMIT_UNCERTAINTY,
        state_summary="current_preoccupation: 仍在想刚才那个边界条件",
        recent_conversation=(
            LLMMessage(MessageRole.USER, "我们继续看这个设计。"),
            LLMMessage(MessageRole.ASSISTANT, "嗯，先看边界条件。"),
        ),
        created_at=datetime(2026, 9, 3, 2, 0, tzinfo=UTC),
        sequence_index=sequence_index,
    )


def test_spontaneity_defaults_use_bounded_episode_policy() -> None:
    config = SpontaneityConfig()

    assert config.min_delay == timedelta(seconds=1)
    assert config.max_delay == timedelta(seconds=30)
    assert config.chain_min_delay == timedelta(seconds=3)
    assert config.chain_max_delay == timedelta(seconds=15)
    assert config.cooldown == timedelta(minutes=2)
    assert isinf(config.max_messages_per_24h)
    assert config.max_followups_per_episode == 7
    assert config.min_motivation == 0.45
    assert config.direct_answer_sample_rate == 0.55
    assert config.short_answer_sample_rate == 0.20
    assert config.interrupt_sample_rate == 0.12
    assert config.interrupt_grace_seconds == 1.25
    assert len(config.continuation_sample_rates) >= 6
    assert all(
        later <= earlier
        for earlier, later in zip(
            config.continuation_sample_rates,
            config.continuation_sample_rates[1:],
            strict=False,
        )
    )


def test_spontaneity_gate_skips_trivial_and_photo_turns() -> None:
    gate = SpontaneityOpportunityGate()

    assert not gate.eligible(
        turn_id="turn_short",
        policy_act=ConversationAct.SHORT_ANSWER,
        user_text="早",
        assistant_text="早。",
    )
    assert not gate.eligible(
        turn_id="turn_photo",
        policy_act=ConversationAct.EMOTIONAL_RESPONSE,
        user_text="[User sent a photo]",
        assistant_text="这张图我看到了。",
        user_images_count=1,
    )


def test_spontaneity_gate_accepts_expressive_turns_and_delay_is_stable() -> None:
    gate = SpontaneityOpportunityGate()

    assert gate.eligible(
        turn_id="turn_expressive",
        policy_act=ConversationAct.ADMIT_UNCERTAINTY,
        user_text="你确定吗？",
        assistant_text="……不，我刚才说得太满了。",
    )
    first = gate.delay_for_turn("turn_expressive")
    second = gate.delay_for_turn("turn_expressive")
    assert first == second
    assert 1.0 <= first <= 30.0

    chain_first = gate.delay_for_followup("turn_expressive", 2)
    chain_second = gate.delay_for_followup("turn_expressive", 2)
    assert chain_first == chain_second
    assert 3.0 <= chain_first <= 15.0


def test_episode_depth_gate_is_bounded_and_deterministic() -> None:
    config = SpontaneityConfig(
        continuation_sample_rates=(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    )
    gate = SpontaneityOpportunityGate(config)

    assert gate.followup_allowed("turn_episode", 1)
    for sequence_index in range(2, 8):
        assert gate.followup_allowed("turn_episode", sequence_index)
    assert not gate.followup_allowed("turn_episode", 8)

    default_gate = SpontaneityOpportunityGate()
    first = default_gate.followup_allowed("turn_episode", 3)
    assert default_gate.followup_allowed("turn_episode", 3) is first


def test_short_answer_and_direct_answer_sampling_are_deterministic() -> None:
    short_gate = SpontaneityOpportunityGate(
        SpontaneityConfig(short_answer_sample_rate=1.0)
    )
    assert short_gate.eligible(
        turn_id="turn_short_sample",
        policy_act=ConversationAct.SHORT_ANSWER,
        user_text="你真的这么想？",
        assistant_text="……至少现在是。",
    )

    direct_gate = SpontaneityOpportunityGate(
        SpontaneityConfig(direct_answer_sample_rate=0.5)
    )
    kwargs = {
        "turn_id": "turn_direct_sample",
        "policy_act": ConversationAct.DIRECT_ANSWER,
        "user_text": "这个实现为什么需要把两个时间尺度拆开处理？",
        "assistant_text": "因为短期补充和长时间无人聊天后的主动联系承担的是不同的交互语义。"
        * 2,
    }

    first = direct_gate.eligible(**kwargs)
    assert direct_gate.eligible(**kwargs) is first


def test_interrupt_window_sampling_is_deterministic() -> None:
    always = SpontaneityOpportunityGate(SpontaneityConfig(interrupt_sample_rate=1.0))
    never = SpontaneityOpportunityGate(SpontaneityConfig(interrupt_sample_rate=0.0))

    assert always.interrupt_window_allowed("turn_interrupt")
    assert not never.interrupt_window_allowed("turn_interrupt")
    first = SpontaneityOpportunityGate().interrupt_window_allowed("turn_interrupt")
    assert SpontaneityOpportunityGate().interrupt_window_allowed("turn_interrupt") is first


def test_spontaneity_composer_accepts_grounded_tangent() -> None:
    async def scenario() -> None:
        provider = FixedProvider(
            """{
              "action": "CONTINUE",
              "motivation": 0.78,
              "focus": "jump sideways to a recent boundary concern",
              "reason_label": "tangent_afterthought",
              "text": "……话说回来，刚才提到的并发边界，我还是有点在意。"
            }"""
        )
        composer = SpontaneityComposer(
            provider=provider,
            persona=load_persona_core(Path("profiles/v2/persona_core.json")),
        )

        decision = await composer.compose(_opportunity(sequence_index=2))

        assert decision.action is SpontaneityAction.CONTINUE
        assert decision.motivation == 0.78
        assert "并发边界" in decision.text
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.metadata["prompt_version"] == SPONTANEITY_PROMPT_VERSION
        assert request.metadata["sequence_index"] == 2
        rendered = "\n".join(message.content for message in request.messages)
        assert "latest_assistant_utterance" in rendered
        assert "sequence_index" in rendered
        assert "mildly unrelated tangent" in rendered
        assert "Do not manufacture another message" in rendered

    asyncio.run(scenario())


def test_spontaneity_composer_low_motivation_and_invalid_output_fail_silent() -> None:
    async def scenario() -> None:
        persona = load_persona_core(Path("profiles/v2/persona_core.json"))
        low = SpontaneityComposer(
            provider=FixedProvider(
                '{"action":"CONTINUE","motivation":0.30,"focus":"x",'
                '"reason_label":"weak","text":"补一句。"}'
            ),
            persona=persona,
        )
        invalid = SpontaneityComposer(
            provider=FixedProvider("not json"),
            persona=persona,
        )

        low_decision = await low.compose(_opportunity())
        invalid_decision = await invalid.compose(_opportunity())

        assert low_decision.action is SpontaneityAction.SILENT
        assert low_decision.reason_label == "low_motivation"
        assert invalid_decision.action is SpontaneityAction.SILENT
        assert invalid_decision.reason_label == "spontaneity_failure"

    asyncio.run(scenario())
