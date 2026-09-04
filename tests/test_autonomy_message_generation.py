import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amadeus_bot.character import (
    AUTONOMY_MESSAGE_PROMPT_VERSION,
    AutonomyAction,
    AutonomyDecision,
    AutonomyMessageGenerationError,
    AutonomyMessageGenerator,
    AutonomyOpportunity,
    AutonomySignal,
    AutonomySignalKind,
    load_persona_core,
)
from amadeus_bot.llm import LLMMessage, LLMRequest, LLMResponse, MessageRole


class CaptureProvider:
    def __init__(self, text: str = "  那个话题……你要是还想继续，我倒也可以再听听。  ") -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="fake-autonomy-message")


def _opportunity() -> AutonomyOpportunity:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    return AutonomyOpportunity(
        now=now,
        last_user_message_at=now - timedelta(hours=3),
        signals=(
            AutonomySignal(
                signal_id="open-thread:thread_1",
                kind=AutonomySignalKind.OPEN_THREAD,
                summary="用户之前说想继续讨论角色记忆边界。",
                salience=0.9,
                source_thread_id="thread_1",
            ),
            AutonomySignal(
                signal_id="memory:unselected",
                kind=AutonomySignalKind.MEMORY,
                summary="不应出现在生成 brief 里的未选中信号。",
                salience=0.8,
                source_memory_id="unselected",
            ),
        ),
        current_state_summary="relationship_tone: familiar",
        relationship_summary="familiar",
    )


def _decision() -> AutonomyDecision:
    return AutonomyDecision(
        action=AutonomyAction.FOLLOW_UP,
        selected_signal_id="open-thread:thread_1",
        motivation=0.82,
        focus="自然地接回之前没聊完的话题",
        reason_label="salient_open_thread",
    )


def test_autonomy_message_generation_uses_only_selected_grounded_signal() -> None:
    async def scenario() -> None:
        provider = CaptureProvider()
        persona = load_persona_core(Path("profiles/v2/persona_core.json"))
        generator = AutonomyMessageGenerator(provider=provider, persona=persona)

        response = await generator.generate(
            decision=_decision(),
            opportunity=_opportunity(),
            recent_conversation=(LLMMessage(MessageRole.USER, "之后再继续聊这个。"),),
        )

        assert response.text == "那个话题……你要是还想继续，我倒也可以再听听。"
        assert len(provider.requests) == 1
        request = provider.requests[0]
        assert request.metadata["prompt_version"] == AUTONOMY_MESSAGE_PROMPT_VERSION
        rendered = "\n".join(message.content for message in request.messages)
        assert "用户之前说想继续讨论角色记忆边界。" in rendered
        assert "不应出现在生成 brief 里的未选中信号。" not in rendered
        assert "do not pretend the user just sent a new message" in rendered

    asyncio.run(scenario())


def test_autonomy_message_generation_rejects_silent_decision() -> None:
    async def scenario() -> None:
        provider = CaptureProvider()
        persona = load_persona_core(Path("profiles/v2/persona_core.json"))
        generator = AutonomyMessageGenerator(provider=provider, persona=persona)
        decision = AutonomyDecision(
            action=AutonomyAction.SILENT,
            motivation=0.0,
            reason_label="silent",
        )

        with pytest.raises(AutonomyMessageGenerationError, match="SILENT"):
            await generator.generate(decision=decision, opportunity=_opportunity())
        assert provider.requests == []

    asyncio.run(scenario())
