import asyncio
import sqlite3
from pathlib import Path

import pytest

from amadeus_bot.memory.migration import (
    LegacyMigrationError,
    file_sha256,
    rehearse_legacy_v1_memory_migration,
)


def _create_empty_legacy_db(path: Path) -> None:
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
            CREATE TABLE memory_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              memory_id INTEGER,
              action TEXT NOT NULL,
              detail TEXT,
              created_at INTEGER NOT NULL
            );
            """
        )
    db.close()


def test_rehearsal_accepts_valid_empty_legacy_database(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "empty-legacy.sqlite"
        target = tmp_path / "target" / "structured-memory.sqlite"
        _create_empty_legacy_db(source)
        source_hash = file_sha256(source)

        report = await rehearse_legacy_v1_memory_migration(
            source_copy=source,
            target_db=target,
            chat_id=42,
        )

        assert report.source_chat_count == 0
        assert report.source_rows == 0
        assert report.target_records == 0
        assert report.memory_enabled is True
        assert report.active_rows == 0
        assert report.forgotten_rows == 0
        assert report.expired_rows == 0
        assert report.sensitive_flagged_rows == 0
        assert report.first_pass.created == 0
        assert report.first_pass.updated == 0
        assert report.first_pass.unchanged == 0
        assert report.first_pass.verified == 0
        assert report.second_pass.created == 0
        assert report.second_pass.updated == 0
        assert report.second_pass.unchanged == 0
        assert report.second_pass.verified == 0
        assert report.source_unchanged is True
        assert file_sha256(source) == source_hash
        assert target.is_file()

        db = sqlite3.connect(target)
        try:
            count = db.execute("SELECT COUNT(*) FROM memory_records").fetchone()
        finally:
            db.close()
        assert count == (0,)
        assert any("zero-record rehearsal" in note for note in report.notes)

    asyncio.run(scenario())


def test_rehearsal_still_rejects_absent_chat_when_other_chat_exists(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "legacy.sqlite"
        target = tmp_path / "target" / "structured-memory.sqlite"
        _create_empty_legacy_db(source)
        db = sqlite3.connect(source)
        with db:
            db.execute(
                "INSERT INTO memory_settings(chat_id, enabled, updated_at) VALUES ('99', 1, 1)"
            )
        db.close()

        with pytest.raises(LegacyMigrationError, match="selected chat_id does not exist"):
            await rehearse_legacy_v1_memory_migration(
                source_copy=source,
                target_db=target,
                chat_id=42,
            )

        assert not target.exists()

    asyncio.run(scenario())
