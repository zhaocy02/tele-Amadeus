from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from amadeus_bot.runtime import AutonomyPreferences, SQLiteRuntimePreferenceStore


def test_autonomy_preferences_default_off_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "preferences.sqlite"
    store = SQLiteRuntimePreferenceStore(path)
    try:
        initial = store.autonomy_preferences(10)
        assert initial.enabled is False
        assert initial.spontaneity_enabled is False
        assert initial.do_not_disturb is False
        assert initial.sleep_mode is False
        assert initial.sleep_started_at is None
        assert initial.updated_at is None

        store.set_autonomy_enabled(10, True)
        store.set_spontaneity_enabled(10, True)
        store.set_autonomy_dnd(10, True)
        changed = store.autonomy_preferences(10)
        assert changed.enabled is True
        assert changed.spontaneity_enabled is True
        assert changed.do_not_disturb is True
        assert changed.updated_at is not None
    finally:
        store.close()

    reopened = SQLiteRuntimePreferenceStore(path)
    try:
        persisted = reopened.autonomy_preferences(10)
        assert persisted.enabled is True
        assert persisted.spontaneity_enabled is True
        assert persisted.do_not_disturb is True
        assert persisted.sleep_mode is False
    finally:
        reopened.close()


def test_spontaneity_column_migrates_existing_autonomy_database(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "preferences.sqlite"
    db = sqlite3.connect(path)
    try:
        db.execute(
            """
            CREATE TABLE autonomy_preferences (
              chat_id TEXT PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
              do_not_disturb INTEGER NOT NULL DEFAULT 0 CHECK(do_not_disturb IN (0, 1)),
              quiet_enabled INTEGER NOT NULL DEFAULT 1 CHECK(quiet_enabled IN (0, 1)),
              quiet_start_minute INTEGER NOT NULL DEFAULT 0,
              quiet_end_minute INTEGER NOT NULL DEFAULT 480,
              sleep_mode INTEGER NOT NULL DEFAULT 0 CHECK(sleep_mode IN (0, 1)),
              sleep_started_at INTEGER,
              updated_at INTEGER NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO autonomy_preferences(chat_id, enabled, updated_at) VALUES ('10', 1, 1)"
        )
        db.commit()
    finally:
        db.close()

    store = SQLiteRuntimePreferenceStore(path)
    try:
        migrated = store.autonomy_preferences(10)
        assert migrated.enabled is True
        assert migrated.spontaneity_enabled is False
        store.set_spontaneity_enabled(10, True)
        assert store.autonomy_preferences(10).spontaneity_enabled is True
    finally:
        store.close()


def test_semantic_sleep_session_tracks_goodnight_until_good_morning(tmp_path: Path) -> None:
    store = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
    bedtime = datetime(2026, 9, 2, 17, 15, tzinfo=UTC)
    try:
        store.set_autonomy_enabled(10, True)
        control_updated_at = store.autonomy_preferences(10).updated_at

        transition = store.observe_user_text(10, "那我先睡了，晚安！", at=bedtime)
        sleeping = store.autonomy_preferences(10)
        assert transition == "sleep_started"
        assert sleeping.sleep_mode is True
        assert sleeping.sleep_started_at == bedtime
        assert sleeping.updated_at == control_updated_at

        assert store.observe_user_text(10, "晚安", at=bedtime + timedelta(minutes=2)) is None

        transition = store.observe_user_text(
            10,
            "早上好！",
            at=bedtime + timedelta(hours=8),
        )
        awake = store.autonomy_preferences(10)
        assert transition == "sleep_ended"
        assert awake.sleep_mode is False
        assert awake.sleep_started_at is None
        assert awake.updated_at == control_updated_at
    finally:
        store.close()


def test_semantic_sleep_aliases_and_false_positive_guard(tmp_path: Path) -> None:
    store = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
    now = datetime(2026, 9, 2, 17, 15, tzinfo=UTC)
    try:
        assert store.observe_user_text(10, "今晚安排行程", at=now) is None
        assert store.observe_user_text(10, "哦呀斯密～", at=now) == "sleep_started"
        transition = store.observe_user_text(
            10,
            "おはよう！",
            at=now + timedelta(hours=8),
        )
        assert transition == "sleep_ended"
    finally:
        store.close()


def test_legacy_quiet_hours_storage_remains_backward_compatible() -> None:
    preferences = AutonomyPreferences(
        quiet_start_minute=23 * 60,
        quiet_end_minute=7 * 60 + 30,
    )
    timezone = ZoneInfo("Asia/Shanghai")

    assert preferences.quiet_active(
        datetime(2026, 9, 2, 17, 30, tzinfo=UTC),
        timezone=timezone,
    )
    assert not preferences.quiet_active(
        datetime(2026, 9, 2, 4, 0, tzinfo=UTC),
        timezone=timezone,
    )


def test_invalid_legacy_quiet_window_is_rejected(tmp_path: Path) -> None:
    store = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
    try:
        with pytest.raises(ValueError, match="start and end"):
            store.set_autonomy_quiet_hours(10, start_minute=60, end_minute=60)
    finally:
        store.close()
