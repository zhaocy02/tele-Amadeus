import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.character import AutonomyAction
from amadeus_bot.runtime import SQLiteAutonomyRuntimeStore
from amadeus_bot.telemetry_cli import _autonomy_report


def test_autonomy_observation_report_is_content_light(
    tmp_path: Path,
    capsys: object,
) -> None:
    data_dir = tmp_path / "v2"
    store = SQLiteAutonomyRuntimeStore(data_dir / "autonomy.sqlite")
    try:
        now = datetime.now(UTC)
        silent = store.record_evaluation(
            10,
            1,
            action=AutonomyAction.SILENT,
            reason_label="guard_user_recently_active",
            selected_signal_id=None,
            motivation=0.0,
            at=now - timedelta(minutes=2),
        )
        candidate = store.record_evaluation(
            10,
            1,
            action=AutonomyAction.FOLLOW_UP,
            reason_label="grounded_follow_up",
            selected_signal_id="open-thread:abc",
            motivation=0.9,
            at=now - timedelta(minutes=1),
        )
        store.record_delivery(
            10,
            1,
            action=AutonomyAction.FOLLOW_UP,
            signal_id="open-thread:abc",
            at=now,
            evaluation_id=candidate.evaluation_id,
        )
        assert silent.evaluation_id != candidate.evaluation_id
    finally:
        store.close()

    args = argparse.Namespace(
        data_dir=data_dir,
        since="24h",
        chat_id=10,
        recent=10,
    )
    _autonomy_report(args)
    output = capsys.readouterr().out  # type: ignore[attr-defined]

    assert "evaluations=2" in output
    assert "silent=1 (50.0%)" in output
    assert "non_silent_candidates=1" in output
    assert "confirmed_deliveries=1" in output
    assert "FOLLOW_UP:1" in output
    assert "SILENT:1" in output
    assert "grounded_follow_up:1" in output
    assert "open-thread:abc" in output
