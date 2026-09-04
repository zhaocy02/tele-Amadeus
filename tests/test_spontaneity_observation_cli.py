import argparse
from datetime import UTC, datetime, timedelta

from amadeus_bot.character import SpontaneityAction
from amadeus_bot.runtime import SQLiteSpontaneityStore
from amadeus_bot.telemetry_cli import _spontaneity_report


def test_spontaneity_observation_reports_silent_candidates_and_deliveries(
    tmp_path,
    capsys,
) -> None:
    data_dir = tmp_path / "v2"
    data_dir.mkdir()
    store = SQLiteSpontaneityStore(data_dir / "spontaneity.sqlite")
    now = datetime.now(UTC)
    try:
        store.record_evaluation(
            42,
            1,
            source_turn_id="turn_silent",
            action=SpontaneityAction.SILENT,
            reason_label="nothing_new",
            motivation=0.0,
            delay_seconds=20.0,
            at=now - timedelta(minutes=4),
        )
        continued = store.record_evaluation(
            42,
            1,
            source_turn_id="turn_continue",
            action=SpontaneityAction.CONTINUE,
            reason_label="afterthought",
            motivation=0.82,
            delay_seconds=41.5,
            at=now - timedelta(minutes=2),
        )
        store.record_delivery(
            42,
            1,
            source_turn_id="turn_continue",
            evaluation_id=continued.evaluation_id,
            at=now - timedelta(minutes=1),
        )
    finally:
        store.close()

    _spontaneity_report(
        argparse.Namespace(
            since="24h",
            chat_id=None,
            recent=10,
            data_dir=data_dir,
        )
    )
    output = capsys.readouterr().out

    assert "Amadeus Phase 5.6.2 spontaneity observation" in output
    assert "evaluations=2" in output
    assert "silent=1 (50.0%)" in output
    assert "continue_candidates=1" in output
    assert "confirmed_deliveries=1" in output
    assert "afterthought:1" in output
    assert "source_turn=turn_continue" in output
