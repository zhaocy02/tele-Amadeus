from datetime import UTC, datetime, timedelta

from amadeus_bot.character import SpontaneityAction
from amadeus_bot.runtime import SQLiteSpontaneityStore


def _at(hour: int = 12) -> datetime:
    return datetime(2026, 9, 3, hour, 0, tzinfo=UTC)


def test_spontaneity_store_records_evaluation_once_per_source_turn(tmp_path) -> None:
    store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
    try:
        first = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_1",
            action=SpontaneityAction.CONTINUE,
            reason_label="afterthought",
            motivation=0.8,
            delay_seconds=32.5,
            at=_at(),
        )
        second = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_1",
            action=SpontaneityAction.SILENT,
            reason_label="duplicate_should_not_replace",
            motivation=0.0,
            delay_seconds=80.0,
            at=_at(13),
        )

        assert second == first
        listed = store.list_evaluations_since(_at() - timedelta(hours=1))
        assert listed == (first,)
    finally:
        store.close()


def test_spontaneity_delivery_stats_are_rolling_and_idempotent(tmp_path) -> None:
    store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
    try:
        evaluation = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_1",
            action=SpontaneityAction.CONTINUE,
            reason_label="afterthought",
            motivation=0.8,
            delay_seconds=25.0,
            at=_at(),
        )
        first = store.record_delivery(
            42,
            1,
            source_turn_id="turn_1",
            evaluation_id=evaluation.evaluation_id,
            at=_at(),
        )
        duplicate = store.record_delivery(
            42,
            1,
            source_turn_id="turn_1",
            evaluation_id=evaluation.evaluation_id,
            at=_at(13),
        )
        store.record_delivery(
            42,
            1,
            source_turn_id="turn_old",
            at=_at() - timedelta(hours=25),
        )

        assert duplicate == first
        stats = store.delivery_stats(42, 1, now=_at(14))
        assert stats.messages_last_24h == 1
        assert stats.last_delivery_at == _at()
    finally:
        store.close()
