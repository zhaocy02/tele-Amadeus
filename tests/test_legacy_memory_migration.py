import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amadeus_bot.memory import MemoryKind, MemoryStatus, StructuredMemoryRepository
from amadeus_bot.memory.migration import (
    LegacyMigrationError,
    LegacyV1MemoryMigrator,
    LegacyV1MemoryReader,
    file_sha256,
    rehearse_legacy_v1_memory_migration,
)


def _create_legacy_db(path: Path) -> None:
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


def _insert_memory(
    db: sqlite3.Connection,
    *,
    memory_id: int,
    chat_id: int,
    kind: str,
    content: str,
    source_type: str = "manual",
    importance: float = 0.8,
    created_at: int = 1_700_000_000,
    updated_at: int = 1_700_000_100,
    last_accessed_at: int | None = None,
    expires_at: int | None = None,
    status: str = "active",
) -> None:
    db.execute(
        """
        INSERT INTO memory_items(
          id, chat_id, kind, content, normalized_content, importance, source_type,
          created_at, updated_at, last_accessed_at, expires_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            str(chat_id),
            kind,
            content,
            content.casefold(),
            importance,
            source_type,
            created_at,
            updated_at,
            last_accessed_at,
            expires_at,
            status,
        ),
    )


def test_rehearsal_maps_all_kinds_statuses_and_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "legacy-copy.sqlite"
        target = tmp_path / "rehearsal" / "structured-memory.sqlite"
        _create_legacy_db(source)
        at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
        expired_at = int((at - timedelta(days=1)).timestamp())
        db = sqlite3.connect(source)
        with db:
            db.execute(
                "INSERT INTO memory_settings(chat_id, enabled, updated_at) VALUES (?, 0, ?)",
                ("42", 1_700_000_200),
            )
            for memory_id, kind in enumerate(
                ("profile", "preference", "commitment", "event", "explicit"),
                start=1,
            ):
                _insert_memory(
                    db,
                    memory_id=memory_id,
                    chat_id=42,
                    kind=kind,
                    content=f"legacy {kind}",
                    source_type="manual" if memory_id % 2 else "automatic",
                    importance=0.5 + memory_id * 0.05,
                    last_accessed_at=1_700_000_150,
                    status="deleted" if kind == "event" else "active",
                    expires_at=expired_at if kind == "explicit" else None,
                )
        db.close()
        source_hash = file_sha256(source)

        report = await rehearse_legacy_v1_memory_migration(
            source_copy=source,
            target_db=target,
            chat_id=42,
            at=at,
        )

        assert report.source_rows == 5
        assert report.memory_enabled is False
        assert report.first_pass.created == 5
        assert report.first_pass.updated == 0
        assert report.second_pass.created == 0
        assert report.second_pass.updated == 0
        assert report.second_pass.unchanged == 5
        assert report.target_records == 5
        assert report.active_rows == 3
        assert report.forgotten_rows == 1
        assert report.expired_rows == 1
        assert report.source_unchanged is True
        assert file_sha256(source) == source_hash

        repository = StructuredMemoryRepository(target)
        try:
            records = []
            reader = LegacyV1MemoryReader(source)
            try:
                for row in reader.rows(42):
                    mapped = LegacyV1MemoryMigrator.map_row(row, at=at)
                    stored = await repository.get(mapped.memory_id)
                    assert stored == mapped
                    records.append(mapped)
            finally:
                reader.close()
            assert [record.kind for record in records] == [
                MemoryKind.FACT,
                MemoryKind.PREFERENCE,
                MemoryKind.OPEN_THREAD,
                MemoryKind.EPISODE,
                MemoryKind.FACT,
            ]
            assert records[3].status is MemoryStatus.FORGOTTEN
            assert records[4].status is MemoryStatus.EXPIRED
            assert all(record.source_message_ids == () for record in records)
            assert all("legacy:v1" in record.tags for record in records)
        finally:
            repository.close()

    asyncio.run(scenario())


def test_rehearsal_migrates_only_explicitly_selected_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "legacy-copy.sqlite"
        target = tmp_path / "structured-memory.sqlite"
        _create_legacy_db(source)
        db = sqlite3.connect(source)
        with db:
            _insert_memory(
                db,
                memory_id=1,
                chat_id=42,
                kind="preference",
                content="chat 42 preference",
            )
            _insert_memory(
                db,
                memory_id=2,
                chat_id=99,
                kind="profile",
                content="chat 99 profile",
            )
        db.close()

        report = await rehearse_legacy_v1_memory_migration(
            source_copy=source,
            target_db=target,
            chat_id=42,
        )

        assert report.source_chat_count == 2
        assert report.source_rows == 1
        assert report.target_records == 1

    asyncio.run(scenario())


def test_rehearsal_rejects_target_with_unrelated_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "legacy-copy.sqlite"
        target = tmp_path / "structured-memory.sqlite"
        _create_legacy_db(source)
        db = sqlite3.connect(source)
        with db:
            _insert_memory(
                db,
                memory_id=1,
                chat_id=42,
                kind="profile",
                content="profile",
            )
        db.close()

        repository = StructuredMemoryRepository(target)
        reader = LegacyV1MemoryReader(source)
        try:
            row = reader.rows(42)[0]
            mapped = LegacyV1MemoryMigrator.map_row(
                row,
                at=datetime(2026, 9, 2, tzinfo=UTC),
            )
            unrelated = mapped.__class__(
                memory_id="unrelated",
                kind=mapped.kind,
                content="unrelated record",
                confidence=mapped.confidence,
                salience=mapped.salience,
                created_at=mapped.created_at,
                source_message_ids=(),
                source_type=mapped.source_type,
            )
            await repository.upsert(unrelated)
        finally:
            reader.close()
            repository.close()

        with pytest.raises(LegacyMigrationError, match="outside the selected legacy chat"):
            await rehearse_legacy_v1_memory_migration(
                source_copy=source,
                target_db=target,
                chat_id=42,
            )

    asyncio.run(scenario())


def test_reader_fails_closed_on_unknown_legacy_value(tmp_path: Path) -> None:
    source = tmp_path / "legacy-copy.sqlite"
    _create_legacy_db(source)
    db = sqlite3.connect(source)
    with db:
        _insert_memory(
            db,
            memory_id=1,
            chat_id=42,
            kind="mystery",
            content="unknown",
        )
    db.close()

    reader = LegacyV1MemoryReader(source)
    try:
        with pytest.raises(LegacyMigrationError, match="unsupported legacy memory kind"):
            reader.rows(42)
    finally:
        reader.close()


def test_reader_rejects_incomplete_schema(tmp_path: Path) -> None:
    source = tmp_path / "broken.sqlite"
    db = sqlite3.connect(source)
    with db:
        db.execute("CREATE TABLE memory_items(id INTEGER PRIMARY KEY, chat_id TEXT)")
        db.execute("CREATE TABLE memory_settings(chat_id TEXT PRIMARY KEY, enabled INTEGER)")
    db.close()

    with pytest.raises(LegacyMigrationError, match="schema is missing columns"):
        LegacyV1MemoryReader(source)
