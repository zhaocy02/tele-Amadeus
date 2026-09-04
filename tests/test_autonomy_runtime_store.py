from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amadeus_bot.character import AutonomyAction
from amadeus_bot.runtime import SQLiteAutonomyRuntimeStore


def _at(hour: int) -> datetime:
    return datetime(2026, 9, 2, hour, 0, tzinfo=UTC)


def test_opportunity_marker_is_persistent_per_generation(tmp_path: Path) -> None:
    path = tmp_path / "autonomy.sqlite"
    store = SQLiteAutonomyRuntimeStore(path)
    try:
        assert store.last_opportunity_at(42, 1) is None
        store.mark_opportunity(42, 1, at=_at(8))
        assert store.last_opportunity_at(42, 1) == _at(8)
        assert store.last_opportunity_at(42, 2) is None
    finally:
        store.close()

    reopened = SQLiteAutonomyRuntimeStore(path)
    try:
        assert reopened.last_opportunity_at(42, 1) == _at(8)
    finally:
        reopened.close()


def test_delivery_stats_track_daily_cap_and_unanswered_chain(tmp_path: Path) -> None:
    store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
    try:
        first = store.record_delivery(
            42,
            1,
            action=AutonomyAction.FOLLOW_UP,
            signal_id="open-thread:one",
            at=_at(8),
        )
        second = store.record_delivery(
            42,
            1,
            action=AutonomyAction.CALLBACK,
            signal_id="memory:two",
            at=_at(14),
        )
        assert first.delivery_id < second.delivery_id

        before_reply = store.delivery_stats(
            42,
            1,
            now=_at(16),
            last_user_message_at=_at(7),
        )
        assert before_reply.autonomy_messages_last_24h == 2
        assert before_reply.consecutive_unanswered_autonomy == 2
        assert before_reply.last_autonomy_message_at == _at(14)

        after_reply = store.delivery_stats(
            42,
            1,
            now=_at(20),
            last_user_message_at=_at(18),
        )
        assert after_reply.autonomy_messages_last_24h == 2
        assert after_reply.consecutive_unanswered_autonomy == 0

        next_day = store.delivery_stats(
            42,
            1,
            now=_at(20) + timedelta(days=1),
            last_user_message_at=_at(18),
        )
        assert next_day.autonomy_messages_last_24h == 0
    finally:
        store.close()


def test_silent_decision_cannot_be_recorded_as_delivery(tmp_path: Path) -> None:
    store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
    try:
        with pytest.raises(ValueError, match="SILENT"):
            store.record_delivery(
                42,
                1,
                action=AutonomyAction.SILENT,
                signal_id="anything",
                at=_at(8),
            )
    finally:
        store.close()
