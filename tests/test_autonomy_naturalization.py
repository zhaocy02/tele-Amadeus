from datetime import UTC, datetime, timedelta

from amadeus_bot.character import (
    AutonomyGuard,
    AutonomyGuardConfig,
    AutonomyOpportunity,
    AutonomyOpportunityScheduler,
    AutonomyScheduleConfig,
    AutonomySignal,
    AutonomySignalKind,
    CharacterState,
    StateResidue,
)
from amadeus_bot.llm import MessageRole
from amadeus_bot.runtime import AutonomyRuntimeCoordinator, StoredConversationMessage


def _now() -> datetime:
    return datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _signal(*, salience: float = 0.5) -> AutonomySignal:
    return AutonomySignal(
        signal_id="recent-conversation:1",
        kind=AutonomySignalKind.RECENT_CONVERSATION,
        summary="用户刚才留下了一个还可以继续聊的问题。",
        salience=salience,
        source_thread_id="conversation-message:1",
    )


def _guard() -> AutonomyGuard:
    return AutonomyGuard(
        AutonomyGuardConfig(
            min_user_idle=timedelta(minutes=30),
            proactive_cooldown=timedelta(minutes=30),
            max_messages_per_24h=12,
            max_consecutive_unanswered=3,
            max_messages_per_sleep_session=2,
            min_signal_salience=0.5,
            min_model_motivation=0.5,
            idle_drive_max_motivation_bonus=0.12,
        )
    )


def _opportunity(
    *,
    unanswered: int = 0,
    last_autonomy_ago: timedelta | None = None,
    messages_24h: int = 0,
    sleep_mode: bool = False,
    sleep_messages: int = 0,
    salience: float = 0.5,
    idle: timedelta = timedelta(hours=2),
) -> AutonomyOpportunity:
    return AutonomyOpportunity(
        now=_now(),
        signals=(_signal(salience=salience),),
        last_user_message_at=_now() - idle,
        last_autonomy_message_at=(
            None if last_autonomy_ago is None else _now() - last_autonomy_ago
        ),
        autonomy_messages_last_24h=messages_24h,
        consecutive_unanswered_autonomy=unanswered,
        sleep_mode=sleep_mode,
        sleep_messages_since_start=sleep_messages,
    )


def test_naturalized_schedule_is_fifteen_minutes() -> None:
    scheduler = AutonomyOpportunityScheduler(
        AutonomyScheduleConfig(check_interval=timedelta(minutes=15))
    )
    assert not scheduler.is_due(
        now=_now(),
        last_opportunity_at=_now() - timedelta(minutes=14),
    )
    assert scheduler.is_due(
        now=_now(),
        last_opportunity_at=_now() - timedelta(minutes=15),
    )


def test_naturalized_guard_accepts_half_salience_after_thirty_minute_idle() -> None:
    guard = _guard()
    opportunity = AutonomyOpportunity(
        now=_now(),
        signals=(_signal(salience=0.5),),
        last_user_message_at=_now() - timedelta(minutes=30),
    )
    result = guard.evaluate(opportunity)
    assert result.allowed is True
    assert result.eligible_signals[0].salience == 0.5


def test_naturalized_daily_cap_is_twelve() -> None:
    guard = _guard()
    assert guard.evaluate(_opportunity(messages_24h=11)).allowed is True
    blocked = guard.evaluate(_opportunity(messages_24h=12))
    assert blocked.allowed is False
    assert blocked.reason_label == "daily_cap"


def test_unanswered_messages_back_off_30_60_120_then_block() -> None:
    guard = _guard()
    expected = (
        (0, timedelta(minutes=30)),
        (1, timedelta(minutes=60)),
        (2, timedelta(minutes=120)),
    )
    for unanswered, cooldown in expected:
        opportunity = _opportunity(unanswered=unanswered)
        assert guard.effective_proactive_cooldown(opportunity) == cooldown
        just_early = _opportunity(
            unanswered=unanswered,
            last_autonomy_ago=cooldown - timedelta(seconds=1),
        )
        assert guard.evaluate(just_early).reason_label == "proactive_cooldown"
        on_time = _opportunity(unanswered=unanswered, last_autonomy_ago=cooldown)
        assert guard.evaluate(on_time).allowed is True

    blocked = guard.evaluate(
        _opportunity(unanswered=3, last_autonomy_ago=timedelta(hours=24))
    )
    assert blocked.allowed is False
    assert blocked.reason_label == "unanswered_cap"


def test_sleep_session_allows_two_then_blocks_third() -> None:
    guard = _guard()
    assert guard.evaluate(_opportunity(sleep_mode=True, sleep_messages=0)).allowed is True
    assert guard.evaluate(_opportunity(sleep_mode=True, sleep_messages=1)).allowed is True
    blocked = guard.evaluate(_opportunity(sleep_mode=True, sleep_messages=2))
    assert blocked.allowed is False
    assert blocked.reason_label == "sleep_session_cap"


def test_signal_below_half_salience_is_filtered() -> None:
    result = _guard().evaluate(_opportunity(salience=0.49))
    assert result.allowed is False
    assert result.reason_label == "no_salient_signal"


def test_idle_contact_drive_is_monotonic_and_saturates() -> None:
    guard = _guard()
    samples = (
        (timedelta(minutes=30), 0.45),
        (timedelta(hours=1), 0.60),
        (timedelta(hours=2), 0.75),
        (timedelta(hours=4), 0.88),
        (timedelta(hours=8), 0.95),
        (timedelta(hours=12), 0.9625),
        (timedelta(hours=24), 1.00),
        (timedelta(hours=48), 1.00),
        (timedelta(days=7), 1.00),
    )
    drives = [guard.idle_contact_drive(_opportunity(idle=idle)) for idle, _ in samples]
    assert drives == [expected for _, expected in samples]
    assert drives == sorted(drives)


def test_idle_contact_drive_softens_motivation_without_becoming_a_signal() -> None:
    guard = _guard()
    early = _opportunity(idle=timedelta(minutes=30))
    late = _opportunity(idle=timedelta(hours=24))

    assert guard.effective_model_motivation(0.40, early) == 0.454
    assert guard.effective_model_motivation(0.40, late) == 0.52
    assert guard.effective_model_motivation(0.40, early) < guard.config.min_model_motivation
    assert guard.effective_model_motivation(0.40, late) >= guard.config.min_model_motivation

    no_signal = _opportunity(idle=timedelta(hours=48), salience=0.49)
    blocked = guard.evaluate(no_signal)
    assert blocked.allowed is False
    assert blocked.reason_label == "no_salient_signal"


def test_character_state_salience_is_derived_not_a_fixed_scalar() -> None:
    residue = StateResidue(
        text="还在想刚才那个 autonomy 设计是不是太保守了。",
        expires_at=_now() + timedelta(hours=24),
    )
    fresh_state = CharacterState(state_version=3, updated_at=_now() - timedelta(hours=1))
    old_state = CharacterState(state_version=3, updated_at=_now() - timedelta(hours=24))

    fresh = AutonomyRuntimeCoordinator._state_signal(
        fresh_state,
        residue,
        index=0,
        at=_now(),
    )
    older = AutonomyRuntimeCoordinator._state_signal(
        old_state,
        residue,
        index=0,
        at=_now(),
    )

    assert fresh.kind is AutonomySignalKind.STATE_PREOCCUPATION
    assert fresh.salience > older.salience
    assert fresh.salience != 0.65


def test_recent_conversation_residue_salience_decays_with_time() -> None:
    content = "这个主动消息机制之后我还想继续调整，你觉得现在是不是还是有点太保守了？"
    recent_record = StoredConversationMessage(
        message_id=10,
        role=MessageRole.USER,
        content=content,
        created_at=_now() - timedelta(hours=1),
    )
    old_record = StoredConversationMessage(
        message_id=11,
        role=MessageRole.USER,
        content=content,
        created_at=_now() - timedelta(hours=24),
    )

    recent = AutonomyRuntimeCoordinator._conversation_signal(recent_record, at=_now())
    old = AutonomyRuntimeCoordinator._conversation_signal(old_record, at=_now())

    assert recent is not None
    assert old is not None
    assert recent.kind is AutonomySignalKind.RECENT_CONVERSATION
    assert recent.source_thread_id == "conversation-message:10"
    assert recent.salience > old.salience
