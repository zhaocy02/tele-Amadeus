import asyncio
import sqlite3
from pathlib import Path

import pytest

from amadeus_bot.runtime import SQLiteRuntimePreferenceStore, prepare_v2_cutover_data


def _create_legacy_snapshot(path: Path, *, memory_enabled: bool) -> None:
    db = sqlite3.connect(path)
    with db:
        db.executescript(
            """
            CREATE TABLE memory_settings (
              chat_id TEXT PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 1,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE memory_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              content TEXT NOT NULL,
              normalized_content TEXT NOT NULL,
              importance REAL NOT NULL DEFAULT 0.5,
              source_type TEXT NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              last_accessed_at INTEGER,
              expires_at INTEGER,
              status TEXT NOT NULL DEFAULT 'active',
              UNIQUE(chat_id, normalized_content)
            );
            """
        )
        db.execute(
            "INSERT INTO memory_settings(chat_id, enabled, updated_at) VALUES ('42', ?, 1)",
            (int(memory_enabled),),
        )
    db.close()


def test_cutover_preparation_applies_legacy_memory_preference(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "snapshot.sqlite"
        target = tmp_path / "v2"
        _create_legacy_snapshot(source, memory_enabled=False)

        report = await prepare_v2_cutover_data(
            source_snapshot=source,
            target_data_dir=target,
            chat_id=42,
        )

        assert report.migration.source_rows == 0
        assert report.migration.memory_enabled is False
        assert report.memory_preference_applied is True
        assert (target / "structured-memory.sqlite").exists()
        assert (target / "runtime-preferences.sqlite").exists()

        preferences = SQLiteRuntimePreferenceStore(target / "runtime-preferences.sqlite")
        try:
            assert preferences.memory_enabled(42) is False
        finally:
            preferences.close()

    asyncio.run(scenario())


def test_cutover_preparation_refuses_nonempty_target(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "snapshot.sqlite"
        target = tmp_path / "v2"
        _create_legacy_snapshot(source, memory_enabled=True)
        target.mkdir()
        (target / "do-not-overwrite.txt").write_text("existing", encoding="utf-8")

        with pytest.raises(RuntimeError, match="must be empty"):
            await prepare_v2_cutover_data(
                source_snapshot=source,
                target_data_dir=target,
                chat_id=42,
            )

    asyncio.run(scenario())
