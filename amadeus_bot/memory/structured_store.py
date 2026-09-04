from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .models import MemoryKind, MemoryRecord, MemorySourceType, MemoryStatus


class StructuredMemoryRepository:
    """SQLite repository for v2 long-term memory records and correction history."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self._db.close()

    async def get(self, memory_id: str) -> MemoryRecord | None:
        row = self._db.execute(
            "SELECT * FROM memory_records WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    async def upsert(self, memory: MemoryRecord) -> None:
        now = datetime.now(UTC)
        with self._db:
            self._upsert(memory)
            self._audit(memory.memory_id, "upsert", memory.kind.value, now)

    async def list_active(
        self,
        *,
        kinds: tuple[MemoryKind, ...] | None = None,
        limit: int = 100,
        at: datetime | None = None,
    ) -> tuple[MemoryRecord, ...]:
        safe_limit = max(1, min(500, limit))
        reference = at or datetime.now(UTC)
        self._validate_aware(reference)
        params: list[object] = [MemoryStatus.ACTIVE.value, reference.isoformat()]
        where = "status = ? AND (expires_at IS NULL OR julianday(expires_at) > julianday(?))"
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            where += f" AND kind IN ({placeholders})"
            params.extend(kind.value for kind in kinds)
        params.append(safe_limit)
        rows = self._db.execute(
            f"""
            SELECT * FROM memory_records
            WHERE {where}
            ORDER BY salience DESC, confidence DESC, julianday(updated_at) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    async def set_status(
        self,
        memory_id: str,
        status: MemoryStatus,
        *,
        at: datetime | None = None,
        detail: str | None = None,
    ) -> bool:
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        with self._db:
            cursor = self._db.execute(
                "UPDATE memory_records SET status = ?, updated_at = ? WHERE memory_id = ?",
                (status.value, timestamp.isoformat(), memory_id),
            )
            if cursor.rowcount:
                self._audit(memory_id, f"status:{status.value}", detail, timestamp)
        return bool(cursor.rowcount)

    async def supersede(
        self,
        old_memory_id: str,
        replacement: MemoryRecord,
        *,
        at: datetime | None = None,
    ) -> MemoryRecord:
        if old_memory_id == replacement.memory_id:
            raise ValueError("replacement memory must use a new memory_id")
        old = await self.get(old_memory_id)
        if old is None:
            raise KeyError(f"memory not found: {old_memory_id}")
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        updated_replacement = replace(
            replacement,
            status=MemoryStatus.ACTIVE,
            supersedes_id=old_memory_id,
            updated_at=timestamp,
        )
        with self._db:
            self._db.execute(
                "UPDATE memory_records SET status = ?, updated_at = ? WHERE memory_id = ?",
                (MemoryStatus.SUPERSEDED.value, timestamp.isoformat(), old_memory_id),
            )
            self._upsert(updated_replacement)
            self._audit(old_memory_id, "superseded", updated_replacement.memory_id, timestamp)
            self._audit(
                updated_replacement.memory_id,
                "supersedes",
                old_memory_id,
                timestamp,
            )
        return updated_replacement

    async def touch_recalled(
        self,
        memory_ids: tuple[str, ...],
        *,
        at: datetime | None = None,
    ) -> None:
        if not memory_ids:
            return
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        with self._db:
            self._db.executemany(
                "UPDATE memory_records SET last_recalled_at = ? WHERE memory_id = ?",
                ((timestamp.isoformat(), memory_id) for memory_id in memory_ids),
            )

    def _create_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    memory_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    salience REAL NOT NULL,
                    status TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_recalled_at TEXT,
                    expires_at TEXT,
                    supersedes_id TEXT,
                    contradicts_id TEXT,
                    tags_json TEXT NOT NULL,
                    entities_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_records_active_kind
                    ON memory_records(status, kind, salience DESC, updated_at DESC);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    memory_id TEXT NOT NULL REFERENCES memory_records(memory_id) ON DELETE CASCADE,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY(memory_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS memory_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _upsert(self, memory: MemoryRecord) -> None:
        updated_at = memory.effective_updated_at
        self._db.execute(
            """
            INSERT INTO memory_records(
                memory_id, kind, content, confidence, salience, status, source_type,
                created_at, updated_at, last_recalled_at, expires_at, supersedes_id,
                contradicts_id, tags_json, entities_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                kind = excluded.kind,
                content = excluded.content,
                confidence = excluded.confidence,
                salience = excluded.salience,
                status = excluded.status,
                source_type = excluded.source_type,
                updated_at = excluded.updated_at,
                last_recalled_at = excluded.last_recalled_at,
                expires_at = excluded.expires_at,
                supersedes_id = excluded.supersedes_id,
                contradicts_id = excluded.contradicts_id,
                tags_json = excluded.tags_json,
                entities_json = excluded.entities_json
            """,
            (
                memory.memory_id,
                memory.kind.value,
                memory.content,
                memory.confidence,
                memory.salience,
                memory.status.value,
                memory.source_type.value,
                memory.created_at.isoformat(),
                updated_at.isoformat(),
                self._optional_datetime(memory.last_recalled_at),
                self._optional_datetime(memory.expires_at),
                memory.supersedes_id,
                memory.contradicts_id,
                json.dumps(memory.tags, ensure_ascii=False),
                json.dumps(memory.entities, ensure_ascii=False),
            ),
        )
        self._db.execute("DELETE FROM memory_sources WHERE memory_id = ?", (memory.memory_id,))
        self._db.executemany(
            "INSERT INTO memory_sources(memory_id, message_id) VALUES (?, ?)",
            ((memory.memory_id, message_id) for message_id in memory.source_message_ids),
        )

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        source_rows = self._db.execute(
            "SELECT message_id FROM memory_sources WHERE memory_id = ? ORDER BY message_id",
            (row["memory_id"],),
        ).fetchall()
        return MemoryRecord(
            memory_id=str(row["memory_id"]),
            kind=MemoryKind(str(row["kind"])),
            content=str(row["content"]),
            confidence=float(row["confidence"]),
            salience=float(row["salience"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            source_message_ids=tuple(int(item["message_id"]) for item in source_rows),
            status=MemoryStatus(str(row["status"])),
            source_type=MemorySourceType(str(row["source_type"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            last_recalled_at=self._parse_optional_datetime(row["last_recalled_at"]),
            expires_at=self._parse_optional_datetime(row["expires_at"]),
            supersedes_id=self._optional_str(row["supersedes_id"]),
            contradicts_id=self._optional_str(row["contradicts_id"]),
            tags=tuple(str(item) for item in json.loads(str(row["tags_json"]))),
            entities=tuple(str(item) for item in json.loads(str(row["entities_json"]))),
        )

    def _audit(self, memory_id: str, action: str, detail: str | None, at: datetime) -> None:
        self._db.execute(
            "INSERT INTO memory_audit(memory_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (memory_id, action, detail, at.isoformat()),
        )

    @staticmethod
    def _optional_datetime(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _parse_optional_datetime(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value is not None else None

    @staticmethod
    def _optional_str(value: object) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("memory mutation timestamps must be timezone-aware")
