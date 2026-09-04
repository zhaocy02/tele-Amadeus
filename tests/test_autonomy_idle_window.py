from datetime import UTC, datetime, timedelta

import pytest

from amadeus_bot.character import (
    AutonomyGuard,
    AutonomyGuardConfig,
    AutonomyOpportunity,
    AutonomySignal,
    AutonomySignalKind,
)


def _opportunity(*, idle: timedelta) -> AutonomyOpportunity:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    return AutonomyOpportunity(
        now=now,
        last_user_message_at=now - idle,
        signals=(
            AutonomySignal(
                signal_id="open-thread:test",
                kind=AutonomySignalKind.OPEN_THREAD,
                summary="A grounded unfinished topic remains available.",
                salience=0.8,
                source_thread_id="test",
            ),
        ),
    )


def test_guard_accepts_exactly_three_minute_idle_window() -> None:
    guard = AutonomyGuard(
        AutonomyGuardConfig(
            min_user_idle=timedelta(minutes=3),
            min_signal_salience=0.5,
        )
    )

    before = guard.evaluate(_opportunity(idle=timedelta(minutes=2, seconds=59)))
    at_window = guard.evaluate(_opportunity(idle=timedelta(minutes=3)))

    assert before.allowed is False
    assert before.reason_label == "user_recently_active"
    assert at_window.allowed is True
    assert at_window.reason_label == "guard_pass"


def test_guard_rejects_idle_windows_shorter_than_one_minute() -> None:
    with pytest.raises(ValueError, match="min_user_idle must be at least 1 minute"):
        AutonomyGuardConfig(min_user_idle=timedelta(seconds=59))


def test_idle_contact_drive_strengthens_with_longer_silence() -> None:
    guard = AutonomyGuard(
        AutonomyGuardConfig(
            min_user_idle=timedelta(minutes=3),
            idle_drive_max_motivation_bonus=0.25,
        )
    )

    drives = [
        guard.idle_contact_drive(_opportunity(idle=idle))
        for idle in (
            timedelta(minutes=3),
            timedelta(minutes=15),
            timedelta(hours=1),
            timedelta(hours=4),
            timedelta(hours=24),
        )
    ]

    assert drives == sorted(drives)
    assert drives == [0.05, 0.3, 0.6, 0.88, 1.0]
    assert guard.contact_drive_motivation_bonus(
        _opportunity(idle=timedelta(hours=4))
    ) == 0.22
