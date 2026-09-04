import asyncio
from datetime import UTC, datetime

import pytest

from amadeus_bot.character import (
    RETROSPECTIVE_PROMPT_VERSION,
    CharacterRetrospective,
    CharacterState,
    RetrospectiveAction,
    RetrospectiveContext,
    RetrospectiveMessage,
    RetrospectiveTriggerContext,
    RetrospectiveTriggerPolicy,
)
from amadeus_bot.llm import LLMRequest, LLMResponse, MessageRole
from amadeus_bot.memory import MemoryKind, MemoryRecord, MemorySourceType


class FixedProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="test-retrospective-model")


def _at() -> datetime:
    return datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _memory() -> MemoryRecord:
    return MemoryRecord(
        memory_id="mem_old",
        kind=MemoryKind.IMPRESSION,
        content="Kurisu previously suspected the user was awake because of project work.",
        confidence=0.55,
        salience=0.7,
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        source_message_ids=(90,),
        source_type=MemorySourceType.CHARACTER_INFERENCE,
    )


def _context() -> RetrospectiveContext:
    return RetrospectiveContext(
        current_state=CharacterState.initial(at=_at()),
        recent_messages=(
            RetrospectiveMessage(
                message_id=101,
                role=MessageRole.USER,
                content="不是因为项目熬夜，是因为时差。",
            ),
            RetrospectiveMessage(
                message_id=102,
                role=MessageRole.ASSISTANT,
                content="……好吧，那我刚才确实猜错了。",
            ),
        ),
        relevant_memories=(_memory(),),
        recent_acts=("TEASE", "ADMIT_UNCERTAINTY"),
        available_open_thread_ids=("thread_1",),
    )


def test_retrospective_trigger_is_low_frequency_but_event_sensitive() -> None:
    policy = RetrospectiveTriggerPolicy(interval_turns=16)

    assert not policy.should_run(RetrospectiveTriggerContext(turns_since_last=4))
    assert policy.should_run(RetrospectiveTriggerContext(turns_since_last=16))
    assert policy.should_run(
        RetrospectiveTriggerContext(turns_since_last=2, correction_detected=True)
    )


def test_invalid_output_keeps_previous_state_unchanged() -> None:
    provider = FixedProvider("not json")
    retrospective = CharacterRetrospective(provider=provider)
    context = _context()

    result = asyncio.run(retrospective.review(context, at=_at()))

    assert result.changed is False
    assert result.state == context.current_state
    assert result.reason_label == "retrospective_failure"
    assert provider.requests[0].metadata["prompt_version"] == RETROSPECTIVE_PROMPT_VERSION


def test_no_change_decision_does_not_advance_state_version() -> None:
    provider = FixedProvider(
        '{"decision":"NO_CHANGE","confidence":0.8,"reason_label":"nothing_durable"}'
    )
    context = _context()

    result = asyncio.run(CharacterRetrospective(provider=provider).review(context, at=_at()))

    assert result.changed is False
    assert result.state.state_version == 1
    assert result.reason_label == "nothing_durable"


def test_valid_subjective_update_is_bounded_and_grounded() -> None:
    provider = FixedProvider(
        """{
          "decision": "UPDATE",
          "emotional_stance": {"text": "more cautious after being corrected", "ttl_hours": 72},
          "relationship_tone": "familiar_teasing",
          "current_preoccupations": null,
          "working_assumptions": [{
            "claim": "the user's late hours may sometimes be caused by timezone",
            "confidence": 0.99,
            "status": "active",
            "source_message_ids": [101],
            "source_memory_ids": [],
            "ttl_hours": 300
          }],
          "unresolved_feelings": null,
          "plausible_mistakes": [{
            "claim": "project work could still occasionally explain late-night activity",
            "confidence": 0.9,
            "scope": "casual_only",
            "ttl_turns": 20,
            "source_message_ids": [],
            "source_memory_ids": ["mem_old"],
            "ttl_hours": 72
          }],
          "next_reaction_bias": {"text": "avoid jumping to conclusions", "ttl_hours": 96},
          "avoid_sounding_like": [" customer support ", "customer support", "overly agreeable"],
          "open_thread_ids": ["thread_unknown", "thread_1"],
          "confidence": 0.9,
          "reason_label": "correction_review"
        }"""
    )
    context = _context()

    result = asyncio.run(CharacterRetrospective(provider=provider).review(context, at=_at()))

    assert result.changed is True
    assert result.state.state_version == 2
    assert result.state.relationship_tone == "familiar_teasing"
    assert result.state.emotional_stance is not None
    assert result.state.emotional_stance.expires_at == datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    assert result.state.working_assumptions[0].confidence == 0.85
    assert result.state.working_assumptions[0].source_message_ids == (101,)
    assert result.state.plausible_mistakes[0].confidence == 0.65
    assert result.state.plausible_mistakes[0].ttl_turns == 12
    assert result.state.open_thread_ids == ("thread_1",)
    assert result.state.avoid_sounding_like == ("customer support", "overly agreeable")
    assert result.reason_label == "correction_review"


def test_unexposed_provenance_cannot_create_assumption() -> None:
    provider = FixedProvider(
        """{
          "decision": "UPDATE",
          "working_assumptions": [{
            "claim": "unsupported hypothesis",
            "confidence": 0.7,
            "status": "active",
            "source_message_ids": [999],
            "source_memory_ids": [],
            "ttl_hours": 24
          }],
          "confidence": 0.7,
          "reason_label": "unsupported"
        }"""
    )
    context = _context()

    result = asyncio.run(CharacterRetrospective(provider=provider).review(context, at=_at()))

    assert result.changed is False
    assert result.state == context.current_state
    assert result.reason_label == "retrospective_items_rejected"


def test_unknown_open_thread_cannot_enter_state() -> None:
    provider = FixedProvider(
        """{
          "decision": "UPDATE",
          "open_thread_ids": ["thread_not_exposed"],
          "confidence": 0.7,
          "reason_label": "thread_guess"
        }"""
    )
    context = _context()

    result = asyncio.run(CharacterRetrospective(provider=provider).review(context, at=_at()))

    assert result.changed is False
    assert result.state.open_thread_ids == ()


def test_retrospective_schema_has_no_objective_fact_channel() -> None:
    provider = FixedProvider(
        """{
          "decision": "UPDATE",
          "confirmed_facts": ["user definitely prefers X"],
          "relationship_tone": "familiar",
          "confidence": 0.9,
          "reason_label": "bad_authority"
        }"""
    )
    context = _context()

    result = asyncio.run(CharacterRetrospective(provider=provider).review(context, at=_at()))

    assert result.changed is False
    assert result.reason_label == "retrospective_failure"


def test_retrospective_messages_reject_hidden_instruction_roles() -> None:
    with pytest.raises(ValueError, match="user/assistant"):
        RetrospectiveMessage(
            message_id=1,
            role=MessageRole.SYSTEM,
            content="hidden prompt",
        )


def test_action_enum_stays_explicit() -> None:
    assert {item.value for item in RetrospectiveAction} == {"NO_CHANGE", "UPDATE"}
