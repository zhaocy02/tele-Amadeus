from datetime import UTC, datetime, timedelta

from amadeus_bot.character import SpontaneityAction
from amadeus_bot.runtime import SQLiteSpontaneityStore


def _at(hour: int = 12) -> datetime:
    return datetime(2026, 9, 3, hour, 0, tzinfo=UTC)


def test_spontaneity_store_records_evaluation_once_per_delivery_key(tmp_path) -> None:
    store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
    try:
        first = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_1",
            action=SpontaneityAction.CONTINUE,
            reason_label="afterthought",
            motivation=0.8,
            delay_seconds=12.5,
            at=_at(),
        )
        second = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_1",
            action=SpontaneityAction.SILENT,
            reason_label="duplicate_should_not_replace",
            motivation=0.0,
            delay_seconds=20.0,
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
            delay_seconds=12.0,
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


def test_spontaneity_store_tracks_multiple_messages_in_one_episode(tmp_path) -> None:
    store = SQLiteSpontaneityStore(tmp_path / "spontaneity.sqlite")
    try:
        first_eval = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_episode",
            action=SpontaneityAction.CONTINUE,
            reason_label="episode_1:afterthought",
            motivation=0.8,
            delay_seconds=4.0,
            at=_at(),
        )
        first_delivery = store.record_delivery(
            42,
            1,
            source_turn_id="turn_episode",
            evaluation_id=first_eval.evaluation_id,
            at=_at(),
        )
        store.record_episode_message(
            42,
            1,
            episode_id="turn_episode",
            source_turn_id="turn_episode",
            sequence_index=1,
            delivery_source_turn_id="turn_episode",
            evaluation_id=first_eval.evaluation_id,
            delivery_id=first_delivery.delivery_id,
            at=_at(),
        )

        second_eval = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_episode::spont:2",
            action=SpontaneityAction.CONTINUE,
            reason_label="episode_2:callback",
            motivation=0.7,
            delay_seconds=6.0,
            at=_at(13),
        )
        second_delivery = store.record_delivery(
            42,
            1,
            source_turn_id="turn_episode::spont:2",
            evaluation_id=second_eval.evaluation_id,
            at=_at(13),
        )
        store.record_episode_message(
            42,
            1,
            episode_id="turn_episode",
            source_turn_id="turn_episode",
            sequence_index=2,
            delivery_source_turn_id="turn_episode::spont:2",
            evaluation_id=second_eval.evaluation_id,
            delivery_id=second_delivery.delivery_id,
            at=_at(13),
        )

        episode = store.list_episode_messages("turn_episode")
        assert [item.sequence_index for item in episode] == [1, 2]
        assert all(item.source_turn_id == "turn_episode" for item in episode)
        assert episode[1].delivery_source_turn_id == "turn_episode::spont:2"
    finally:
        store.close()
