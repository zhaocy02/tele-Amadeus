import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from amadeus_bot.character import (
    AUTONOMY_PROMPT_VERSION,
    AutonomyAction,
    AutonomyGuard,
    AutonomyGuardConfig,
    AutonomyOpportunity,
    AutonomyOpportunityScheduler,
    AutonomyPlanner,
    AutonomyScheduleConfig,
    AutonomySignal,
    AutonomySignalKind,
)
from amadeus_bot.llm import LLMRequest, LLMResponse


class FixedProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="test-autonomy-model")


def _now() -> datetime:
    return datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _thread_signal(*, salience: float = 0.8) -> AutonomySignal:
    return AutonomySignal(
        signal_id="sig_thread",
        kind=AutonomySignalKind.OPEN_THREAD,
        summary="The user left the memory architecture discussion unfinished.",
        salience=salience,
        source_thread_id="thread_1",
    )


def _relationship_signal() -> AutonomySignal:
    return AutonomySignal(
        signal_id="sig_relationship",
        kind=AutonomySignalKind.RELATIONSHIP_MEMORY,
        summary="A recurring joke has been established in prior conversation.",
        salience=0.82,
        source_memory_id="mem_relationship",
    )


def _opportunity(
    *,
    signals: tuple[AutonomySignal, ...] | None = None,
    last_user_message_at: datetime | None = None,
    last_autonomy_message_at: datetime | None = None,
    messages_24h: int = 0,
    unanswered: int = 0,
    do_not_disturb: bool = False,
    user_suppressed: bool = False,
) -> AutonomyOpportunity:
    return AutonomyOpportunity(
        now=_now(),
        signals=(_thread_signal(),) if signals is None else signals,
        last_user_message_at=(
            _now() - timedelta(hours=4)
            if last_user_message_at is None
            else last_user_message_at
        ),
        last_autonomy_message_at=last_autonomy_message_at,
        autonomy_messages_last_24h=messages_24h,
        consecutive_unanswered_autonomy=unanswered,
        do_not_disturb=do_not_disturb,
        user_suppressed=user_suppressed,
        current_state_summary="Kurisu is mildly curious about the unfinished design.",
        relationship_summary="familiar technical banter",
    )


def test_opportunity_scheduler_schedules_checks_not_messages() -> None:
    scheduler = AutonomyOpportunityScheduler(
        AutonomyScheduleConfig(check_interval=timedelta(hours=1))
    )
    last = _now() - timedelta(minutes=45)

    assert not scheduler.is_due(now=_now(), last_opportunity_at=last)
    assert scheduler.is_due(
        now=_now(),
        last_opportunity_at=_now() - timedelta(hours=1),
    )
    assert scheduler.next_due(last_opportunity_at=last) == last + timedelta(hours=1)
    assert scheduler.is_due(now=_now(), last_opportunity_at=None)


def test_guard_block_prevents_any_llm_call() -> None:
    provider = FixedProvider(
        '{"action":"FOLLOW_UP","selected_signal_id":"sig_thread",'
        '"motivation":0.9,"focus":"continue thread","reason_label":"thread"}'
    )
    planner = AutonomyPlanner(provider=provider)
    opportunity = AutonomyOpportunity(now=_now(), signals=(_thread_signal(),))

    decision = asyncio.run(planner.decide(opportunity))

    assert decision.action is AutonomyAction.SILENT
    assert decision.reason_label == "guard_no_user_history"
    assert provider.requests == []


@pytest.mark.parametrize(
    ("opportunity", "reason"),
    (
        (_opportunity(user_suppressed=True), "user_suppressed"),
        (_opportunity(do_not_disturb=True), "do_not_disturb"),
        (_opportunity(messages_24h=2), "daily_cap"),
        (_opportunity(unanswered=2), "unanswered_cap"),
        (
            _opportunity(last_user_message_at=_now() - timedelta(minutes=30)),
            "user_recently_active",
        ),
        (
            _opportunity(last_autonomy_message_at=_now() - timedelta(hours=2)),
            "proactive_cooldown",
        ),
        (_opportunity(signals=(_thread_signal(salience=0.2),)), "no_salient_signal"),
    ),
)
def test_guard_reasons_are_explicit(opportunity: AutonomyOpportunity, reason: str) -> None:
    result = AutonomyGuard().evaluate(opportunity)

    assert result.allowed is False
    assert result.reason_label == reason
    assert result.eligible_signals == ()


def test_valid_followup_is_grounded_in_eligible_signal() -> None:
    provider = FixedProvider(
        """{
          "action": "FOLLOW_UP",
          "selected_signal_id": "sig_thread",
          "motivation": 0.82,
          "focus": "return to the unfinished memory architecture question",
          "reason_label": "unfinished_thread"
        }"""
    )
    planner = AutonomyPlanner(provider=provider)
    low_signal = AutonomySignal(
        signal_id="sig_low",
        kind=AutonomySignalKind.STATE_PREOCCUPATION,
        summary="A weak passing thought.",
        salience=0.2,
    )

    decision = asyncio.run(
        planner.decide(_opportunity(signals=(_thread_signal(), low_signal)))
    )

    assert decision.action is AutonomyAction.FOLLOW_UP
    assert decision.selected_signal_id == "sig_thread"
    assert decision.focus == "return to the unfinished memory architecture question"
    assert provider.requests[0].metadata["prompt_version"] == AUTONOMY_PROMPT_VERSION
    prompt = provider.requests[0].messages[1].content
    assert "sig_thread" in prompt
    assert "sig_low" not in prompt


def test_invalid_model_output_fails_closed_to_silent() -> None:
    provider = FixedProvider("not json")

    decision = asyncio.run(AutonomyPlanner(provider=provider).decide(_opportunity()))

    assert decision.action is AutonomyAction.SILENT
    assert decision.reason_label == "autonomy_failure"


def test_model_cannot_select_signal_not_exposed_by_guard() -> None:
    provider = FixedProvider(
        """{
          "action": "ASK",
          "selected_signal_id": "sig_invented",
          "motivation": 0.9,
          "focus": "ask about an invented reason",
          "reason_label": "invented"
        }"""
    )

    decision = asyncio.run(AutonomyPlanner(provider=provider).decide(_opportunity()))

    assert decision.action is AutonomyAction.SILENT
    assert decision.reason_label == "unauthorized_signal"


def test_action_must_match_signal_semantics() -> None:
    provider = FixedProvider(
        """{
          "action": "CALLBACK",
          "selected_signal_id": "sig_thread",
          "motivation": 0.9,
          "focus": "pretend the open thread is an old memory callback",
          "reason_label": "mismatch"
        }"""
    )

    decision = asyncio.run(AutonomyPlanner(provider=provider).decide(_opportunity()))

    assert decision.action is AutonomyAction.SILENT
    assert decision.reason_label == "action_signal_mismatch"


def test_low_motivation_stays_silent_even_after_guard_passes() -> None:
    provider = FixedProvider(
        """{
          "action": "FOLLOW_UP",
          "selected_signal_id": "sig_thread",
          "motivation": 0.3,
          "focus": "continue the open thread",
          "reason_label": "weak_reason"
        }"""
    )

    decision = asyncio.run(AutonomyPlanner(provider=provider).decide(_opportunity()))

    assert decision.action is AutonomyAction.SILENT
    assert decision.reason_label == "low_motivation"


def test_teasing_is_suppressed_after_an_unanswered_autonomy_message() -> None:
    provider = FixedProvider(
        """{
          "action": "TEASE",
          "selected_signal_id": "sig_relationship",
          "motivation": 0.9,
          "focus": "make a light callback to the recurring joke",
          "reason_label": "recurring_joke"
        }"""
    )
    opportunity = _opportunity(
        signals=(_relationship_signal(),),
        unanswered=1,
        last_autonomy_message_at=_now() - timedelta(hours=13),
    )

    decision = asyncio.run(AutonomyPlanner(provider=provider).decide(opportunity))

    assert decision.action is AutonomyAction.SILENT
    assert decision.reason_label == "tease_after_unanswered"


def test_memory_signal_requires_program_owned_source_id() -> None:
    with pytest.raises(ValueError, match="source_memory_id"):
        AutonomySignal(
            signal_id="sig_bad",
            kind=AutonomySignalKind.MEMORY,
            summary="memory-like signal without a memory id",
            salience=0.8,
        )


def test_opportunity_rejects_future_activity_timestamps() -> None:
    with pytest.raises(ValueError, match="future"):
        AutonomyOpportunity(
            now=_now(),
            last_user_message_at=_now() + timedelta(minutes=1),
        )


def test_guard_configuration_is_bounded() -> None:
    with pytest.raises(ValueError, match="at least 30 minutes"):
        AutonomyGuardConfig(proactive_cooldown=timedelta(minutes=5))
