import asyncio
from pathlib import Path

from amadeus_bot.character import (
    ConversationAct,
    ConversationPolicyPlanner,
    PolicyContext,
    load_persona_core,
)
from amadeus_bot.llm import LLMRequest, LLMResponse


class FixedProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            text=(
                '{"act":"DEFLECT","intensity":0.6,"answer_obligation":"partial",'
                '"memory_callback_ids":[],"state_bias":"guarded",'
                '"reason_label":"self_disclosure"}'
            ),
            model="test-policy-model",
        )


def test_direct_non_disclosure_probe_keeps_deliberate_policy() -> None:
    provider = FixedProvider()
    persona = load_persona_core(Path("profiles/v2/persona_core.json")).core
    planner = ConversationPolicyPlanner(provider=provider, persona=persona)

    policy = asyncio.run(
        planner.plan(PolicyContext(user_message="你会有什么不告诉我的小秘密么"))
    )

    assert policy.act is ConversationAct.DEFLECT
    assert len(provider.requests) == 1
