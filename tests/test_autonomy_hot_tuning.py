from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from amadeus_bot.character import (
    AutonomyGuardConfig,
    AutonomyOpportunity,
    AutonomyScheduleConfig,
    AutonomySignal,
    AutonomySignalKind,
)
from amadeus_bot.runtime import (
    RuntimeAutonomyTuningControl,
    SQLiteAutonomyTuningStore,
    SQLiteRuntimePreferenceStore,
    SQLiteSpontaneityStore,
    TunableAutonomyGuard,
    TunableAutonomyOpportunityScheduler,
)
from amadeus_bot.telegram.provider_router import V2TelegramMessageRouter
from amadeus_bot.telegram.v2_observation_router import (
    V2TelegramMessageRouter as ObservationRouter,
)


def _control(tmp_path) -> RuntimeAutonomyTuningControl:  # type: ignore[no-untyped-def]
    return RuntimeAutonomyTuningControl(
        SQLiteAutonomyTuningStore(tmp_path / "runtime-preferences.sqlite")
    )


def _opportunity(now: datetime, *, idle: timedelta) -> AutonomyOpportunity:
    return AutonomyOpportunity(
        now=now,
        last_user_message_at=now - idle,
        signals=(
            AutonomySignal(
                signal_id="state-preoccupation:test",
                kind=AutonomySignalKind.STATE_PREOCCUPATION,
                summary="still thinking about the earlier topic",
                salience=0.9,
            ),
        ),
    )


def test_tuning_defaults_and_persistence_share_runtime_preferences_db(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "runtime-preferences.sqlite"
    preferences = SQLiteRuntimePreferenceStore(path)
    control = RuntimeAutonomyTuningControl(SQLiteAutonomyTuningStore(path))
    try:
        defaults = control.get(1)
        assert defaults.min_user_idle == timedelta(minutes=3)
        assert defaults.check_interval == timedelta(minutes=5)
        assert defaults.max_messages_per_24h == 24
        assert defaults.idle_drive_max_motivation_bonus == 0.25
        assert defaults.spontaneity_max_messages_per_24h is None
        assert not defaults.customized

        preferences.set_autonomy_enabled(1, True)
        control.set_min_user_idle(1, timedelta(minutes=9))
        control.set_check_interval(1, timedelta(minutes=12))
        control.set_daily_cap(1, 18)
        control.set_idle_drive_bonus(1, 0.2)
        control.set_spontaneity_daily_cap(1, 30)
        assert preferences.autonomy_preferences(1).enabled
    finally:
        control.close()
        preferences.close()

    reopened = RuntimeAutonomyTuningControl(SQLiteAutonomyTuningStore(path))
    try:
        saved = reopened.get(1)
        assert saved.min_user_idle == timedelta(minutes=9)
        assert saved.check_interval == timedelta(minutes=12)
        assert saved.max_messages_per_24h == 18
        assert saved.idle_drive_max_motivation_bonus == 0.2
        assert saved.spontaneity_max_messages_per_24h == 30
        assert saved.customized
        assert reopened.get(2).min_user_idle == timedelta(minutes=3)

        reset = reopened.reset(1)
        assert reset.min_user_idle == timedelta(minutes=3)
        assert reset.check_interval == timedelta(minutes=5)
        assert reset.max_messages_per_24h == 24
        assert reset.spontaneity_max_messages_per_24h is None
        assert not reset.customized
    finally:
        reopened.close()


def test_tuning_bounds_fail_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    control = _control(tmp_path)
    try:
        with pytest.raises(ValueError, match="idle"):
            control.set_min_user_idle(1, timedelta(seconds=59))
        with pytest.raises(ValueError, match="interval"):
            control.set_check_interval(1, timedelta(days=8))
        with pytest.raises(ValueError, match="daily cap"):
            control.set_daily_cap(1, 49)
        with pytest.raises(ValueError, match="drive"):
            control.set_idle_drive_bonus(1, 0.51)
        with pytest.raises(ValueError, match="spontaneity daily cap"):
            control.set_spontaneity_daily_cap(1, 721)
    finally:
        control.close()


def test_tunable_guard_applies_per_chat_immediately(tmp_path) -> None:  # type: ignore[no-untyped-def]
    control = _control(tmp_path)
    guard = TunableAutonomyGuard(
        AutonomyGuardConfig(
            min_user_idle=timedelta(minutes=3),
            proactive_cooldown=timedelta(minutes=30),
            max_messages_per_24h=24,
            max_consecutive_unanswered=3,
            max_messages_per_sleep_session=2,
            min_signal_salience=0.5,
            min_model_motivation=0.5,
            idle_drive_max_motivation_bonus=0.25,
        ),
        control,
    )
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    opportunity = _opportunity(now, idle=timedelta(minutes=4))
    try:
        with control.use_chat(1):
            assert guard.evaluate(opportunity).allowed

        control.set_min_user_idle(1, timedelta(minutes=10))
        with control.use_chat(1):
            result = guard.evaluate(opportunity)
            assert not result.allowed
            assert result.reason_label == "user_recently_active"

        with control.use_chat(2):
            assert guard.evaluate(opportunity).allowed

        long_idle = _opportunity(now, idle=timedelta(hours=8))
        control.set_idle_drive_bonus(1, 0.1)
        with control.use_chat(1):
            assert guard.contact_drive_motivation_bonus(long_idle) == pytest.approx(0.095)
        with control.use_chat(2):
            assert guard.contact_drive_motivation_bonus(long_idle) == pytest.approx(0.2375)
    finally:
        control.close()


def test_tunable_scheduler_applies_new_interval_without_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    control = _control(tmp_path)
    scheduler = TunableAutonomyOpportunityScheduler(
        AutonomyScheduleConfig(check_interval=timedelta(minutes=5)),
        control,
    )
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    last = now - timedelta(minutes=6)
    try:
        with control.use_chat(1):
            assert scheduler.is_due(now=now, last_opportunity_at=last)

        control.set_check_interval(1, timedelta(minutes=10))
        with control.use_chat(1):
            assert not scheduler.is_due(now=now, last_opportunity_at=last)
            assert scheduler.next_due(last_opportunity_at=last) == last + timedelta(minutes=10)

        with control.use_chat(2):
            assert scheduler.is_due(now=now, last_opportunity_at=last)
    finally:
        control.close()


def test_autonomy_tune_command_updates_and_resets_without_character_llm(tmp_path) -> None:  # type: ignore[no-untyped-def]
    control = _control(tmp_path)
    router = object.__new__(V2TelegramMessageRouter)
    router._autonomy_tuning = control
    try:
        response = router._autonomy_tune_text(1, "tune idle 7m")
        assert "已热更新并持久化；无需重启" in response
        assert control.get(1).min_user_idle == timedelta(minutes=7)

        response = router._autonomy_tune_text(1, "tune interval 1.5m")
        assert "interval=90s" in response
        assert control.get(1).check_interval == timedelta(seconds=90)

        response = router._autonomy_tune_text(1, "tune daily 20")
        assert "daily=20" in response

        response = router._autonomy_tune_text(1, "tune drive 0.20")
        assert "drive=0.20" in response

        response = router._autonomy_tune_text(1, "tune spontaneous 30")
        assert "spontaneous=30" in response
        response = router._autonomy_tune_text(1, "tune spontaneous unlimited")
        assert "spontaneous=unlimited" in response

        bad = router._autonomy_tune_text(1, "tune daily 100")
        assert bad.startswith("参数无效：")

        reset = router._autonomy_tune_text(1, "tune reset")
        assert "已恢复当前生产默认值；立即生效" in reset
        assert not control.get(1).customized
    finally:
        control.close()


def test_finite_spontaneity_hot_cap_is_enforced_after_base_preflight(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    control = _control(tmp_path)
    store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
    router = object.__new__(V2TelegramMessageRouter)
    router._autonomy_tuning = control
    router._spontaneity_store = store
    monkeypatch.setattr(
        ObservationRouter,
        "_spontaneity_preflight",
        lambda self, chat_id, *, generation, source_user_message_id: True,
    )
    now = datetime.now(UTC)
    try:
        control.set_spontaneity_daily_cap(1, 2)
        assert router._spontaneity_preflight(1, generation=1, source_user_message_id=1)
        store.record_delivery(1, 1, source_turn_id="turn-1", at=now - timedelta(minutes=3))
        assert router._spontaneity_preflight(1, generation=1, source_user_message_id=2)
        store.record_delivery(1, 1, source_turn_id="turn-2", at=now - timedelta(minutes=1))
        assert not router._spontaneity_preflight(1, generation=1, source_user_message_id=3)

        control.set_spontaneity_daily_cap(1, None)
        assert router._spontaneity_preflight(1, generation=1, source_user_message_id=4)
    finally:
        store.close()
        control.close()
