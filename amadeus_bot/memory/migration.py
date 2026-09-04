from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .legacy_store import LegacyMemoryKind, is_sensitive_memory
from .models import MemoryKind, MemoryRecord, MemorySourceType, MemoryStatus
from .structured_store import StructuredMemoryRepository


class LegacyMigrationError(RuntimeError):
    """Raised when a legacy database cannot be migrated without guessing."""


@dataclass(frozen=True, slots=True)
class LegacyV1MemoryRow:
    memory_id: int
    chat_id: str
    kind: LegacyMemoryKind
    content: str
    importance: float
    source_type: str
    created_at: int
    updated_at: int
    last_accessed_at: int | None
    expires_at: int | None
    status: str


@dataclass(frozen=True, slots=True)
class MigrationPassReport:
    created: int
    updated: int
    unchanged: int
    verified: int


@dataclass(frozen=True, slots=True)
class MigrationRehearsalReport:
    source_sha256: str
    source_size_bytes: int
    chat_id: int
    source_chat_count: int
    memory_enabled: bool
    source_rows: int
    active_rows: int
    forgotten_rows: int
    expired_rows: int
    sensitive_flagged_rows: int
    kind_counts: dict[str, int]
    first_pass: MigrationPassReport
    second_pass: MigrationPassReport
    target_records: int
    source_unchanged: bool
    notes: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


_KIND_MAP: dict[LegacyMemoryKind, MemoryKind] = {
    LegacyMemoryKind.PROFILE: MemoryKind.FACT,
    LegacyMemoryKind.PREFERENCE: MemoryKind.PREFERENCE,
    LegacyMemoryKind.COMMITMENT: MemoryKind.OPEN_THREAD,
    LegacyMemoryKind.EVENT: MemoryKind.EPISODE,
    LegacyMemoryKind.EXPLICIT: MemoryKind.FACT,
}
_REQUIRED_MEMORY_COLUMNS = {
    "id",
    "chat_id",
    "kind",
    "content",
    "importance",
    "source_type",
    "created_at",
    "updated_at",
    "last_accessed_at",
    "expires_at",
    "status",
}
_REQUIRED_SETTINGS_COLUMNS = {"chat_id", "enabled"}
_VALID_SOURCE_TYPES = {"manual", "automatic"}
_VALID_STATUSES = {"active", "deleted"}


class LegacyV1MemoryReader:
    """Read the production-compatible v1 memory schema without mutating the source file."""

    def __init__(self, filename: Path) -> None:
        self.path = filename.expanduser().resolve()
        if not self.path.is_file():
            raise LegacyMigrationError(f"legacy source is not a file: {self.path}")
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA query_only = ON")
        self._validate_schema()
        self._validate_integrity()

    def close(self) -> None:
        self._db.close()

    def chat_ids(self) -> tuple[str, ...]:
        rows = self._db.execute(
            """
            SELECT chat_id FROM memory_items
            UNION
            SELECT chat_id FROM memory_settings
            ORDER BY chat_id
            """
        ).fetchall()
        return tuple(str(row["chat_id"]) for row in rows)

    def memory_enabled(self, chat_id: int) -> bool:
        row = self._db.execute(
            "SELECT enabled FROM memory_settings WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        return row is None or int(row["enabled"]) != 0

    def rows(self, chat_id: int) -> tuple[LegacyV1MemoryRow, ...]:
        rows = self._db.execute(
            """
            SELECT id, chat_id, kind, content, importance, source_type,
                   created_at, updated_at, last_accessed_at, expires_at, status
            FROM memory_items
            WHERE chat_id = ?
            ORDER BY id
            """,
            (str(chat_id),),
        ).fetchall()
        return tuple(self._as_row(row) for row in rows)

    def _validate_schema(self) -> None:
        tables = {
            str(row["name"])
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for required in ("memory_items", "memory_settings"):
            if required not in tables:
                raise LegacyMigrationError(f"legacy database is missing table: {required}")
        memory_columns = self._table_columns("memory_items")
        settings_columns = self._table_columns("memory_settings")
        missing_memory = sorted(_REQUIRED_MEMORY_COLUMNS - memory_columns)
        missing_settings = sorted(_REQUIRED_SETTINGS_COLUMNS - settings_columns)
        if missing_memory:
            raise LegacyMigrationError(
                "legacy memory_items schema is missing columns: " + ",".join(missing_memory)
            )
        if missing_settings:
            raise LegacyMigrationError(
                "legacy memory_settings schema is missing columns: " + ",".join(missing_settings)
            )

    def _validate_integrity(self) -> None:
        rows = self._db.execute("PRAGMA quick_check").fetchall()
        results = tuple(str(row[0]) for row in rows)
        if results != ("ok",):
            raise LegacyMigrationError("legacy SQLite quick_check did not return ok")

    def _table_columns(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
        }

    @staticmethod
    def _as_row(row: sqlite3.Row) -> LegacyV1MemoryRow:
        kind_raw = str(row["kind"])
        source_type = str(row["source_type"])
        status = str(row["status"])
        try:
            kind = LegacyMemoryKind(kind_raw)
        except ValueError as exc:
            raise LegacyMigrationError(f"unsupported legacy memory kind: {kind_raw}") from exc
        if source_type not in _VALID_SOURCE_TYPES:
            raise LegacyMigrationError(f"unsupported legacy source_type: {source_type}")
        if status not in _VALID_STATUSES:
            raise LegacyMigrationError(f"unsupported legacy memory status: {status}")
        return LegacyV1MemoryRow(
            memory_id=int(row["id"]),
            chat_id=str(row["chat_id"]),
            kind=kind,
            content=str(row["content"]),
            importance=float(row["importance"]),
            source_type=source_type,
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            last_accessed_at=(
                int(row["last_accessed_at"])
                if row["last_accessed_at"] is not None
                else None
            ),
            expires_at=(
                int(row["expires_at"]) if row["expires_at"] is not None else None
            ),
            status=status,
        )


class LegacyV1MemoryMigrator:
    """Map one explicitly selected v1 chat into the single-user v2 memory repository."""

    def __init__(self, repository: StructuredMemoryRepository) -> None:
        self._repository = repository

    async def migrate(
        self,
        rows: tuple[LegacyV1MemoryRow, ...],
        *,
        at: datetime,
    ) -> MigrationPassReport:
        self._validate_aware(at)
        created = 0
        updated = 0
        unchanged = 0
        expected: list[MemoryRecord] = []
        for row in rows:
            mapped = self.map_row(row, at=at)
            expected.append(mapped)
            existing = await self._repository.get(mapped.memory_id)
            if existing is None:
                await self._repository.upsert(mapped)
                created += 1
            elif existing == mapped:
                unchanged += 1
            else:
                await self._repository.upsert(mapped)
                updated += 1
        verified = await self.verify(tuple(expected))
        return MigrationPassReport(
            created=created,
            updated=updated,
            unchanged=unchanged,
            verified=verified,
        )

    async def verify(self, expected: tuple[MemoryRecord, ...]) -> int:
        for memory in expected:
            stored = await self._repository.get(memory.memory_id)
            if stored != memory:
                raise LegacyMigrationError(
                    f"migrated record verification failed: {memory.memory_id}"
                )
        return len(expected)

    @staticmethod
    def map_row(row: LegacyV1MemoryRow, *, at: datetime) -> MemoryRecord:
        LegacyV1MemoryMigrator._validate_aware(at)
        created_at = datetime.fromtimestamp(row.created_at, tz=UTC)
        updated_at = datetime.fromtimestamp(row.updated_at, tz=UTC)
        expires_at = (
            datetime.fromtimestamp(row.expires_at, tz=UTC)
            if row.expires_at is not None
            else None
        )
        last_recalled_at = (
            datetime.fromtimestamp(row.last_accessed_at, tz=UTC)
            if row.last_accessed_at is not None
            else None
        )
        status = MemoryStatus.ACTIVE
        if row.status == "deleted":
            status = MemoryStatus.FORGOTTEN
        elif expires_at is not None and expires_at <= at:
            status = MemoryStatus.EXPIRED
        confidence = 0.95 if row.source_type == "manual" else 0.75
        chat_hash = hashlib.sha256(row.chat_id.encode("utf-8")).hexdigest()[:12]
        return MemoryRecord(
            memory_id=f"legacy-v1-{chat_hash}-{row.memory_id}",
            kind=_KIND_MAP[row.kind],
            content=row.content.strip(),
            confidence=confidence,
            salience=max(0.0, min(1.0, row.importance)),
            created_at=created_at,
            source_message_ids=(),
            status=status,
            source_type=MemorySourceType.MIGRATED,
            updated_at=updated_at,
            last_recalled_at=last_recalled_at,
            expires_at=expires_at,
            tags=(
                "legacy:v1",
                f"legacy_kind:{row.kind.value}",
                f"legacy_source:{row.source_type}",
                f"legacy_chat_hash:{chat_hash}",
                f"legacy_memory_id:{row.memory_id}",
            ),
        )

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("migration timestamp must be timezone-aware")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_target_records(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT COUNT(*) FROM memory_records").fetchone()
    finally:
        db.close()
    return int(row[0]) if row is not None else 0


async def rehearse_legacy_v1_memory_migration(
    *,
    source_copy: Path,
    target_db: Path,
    chat_id: int,
    at: datetime | None = None,
) -> MigrationRehearsalReport:
    """Run and immediately re-run a migration to prove read-only source use and idempotency."""

    if chat_id <= 0:
        raise ValueError("chat_id must be positive")
    source = source_copy.expanduser().resolve()
    target = target_db.expanduser().resolve()
    if source == target:
        raise LegacyMigrationError("migration source and target must be different files")
    timestamp = at or datetime.now(UTC)
    LegacyV1MemoryMigrator._validate_aware(timestamp)
    source_hash_before = file_sha256(source)
    source_size = source.stat().st_size

    reader = LegacyV1MemoryReader(source)
    try:
        chat_ids = reader.chat_ids()
        if chat_ids and str(chat_id) not in chat_ids:
            raise LegacyMigrationError("selected chat_id does not exist in legacy database")
        rows = reader.rows(chat_id)
        memory_enabled = reader.memory_enabled(chat_id)
    finally:
        reader.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    repository = StructuredMemoryRepository(target)
    try:
        migrator = LegacyV1MemoryMigrator(repository)
        first = await migrator.migrate(rows, at=timestamp)
        second = await migrator.migrate(rows, at=timestamp)
    finally:
        repository.close()

    if second.created or second.updated or second.unchanged != len(rows):
        raise LegacyMigrationError("second migration pass was not idempotent")
    target_records = _count_target_records(target)
    if target_records != len(rows):
        raise LegacyMigrationError(
            "migration target contains records outside the selected legacy chat"
        )

    source_hash_after = file_sha256(source)
    if source_hash_before != source_hash_after or source.stat().st_size != source_size:
        raise LegacyMigrationError("legacy source changed during read-only migration rehearsal")

    kind_counts = {kind.value: 0 for kind in LegacyMemoryKind}
    active_rows = 0
    forgotten_rows = 0
    expired_rows = 0
    sensitive_flagged_rows = 0
    for row in rows:
        kind_counts[row.kind.value] += 1
        mapped = LegacyV1MemoryMigrator.map_row(row, at=timestamp)
        if mapped.status is MemoryStatus.ACTIVE:
            active_rows += 1
        elif mapped.status is MemoryStatus.FORGOTTEN:
            forgotten_rows += 1
        elif mapped.status is MemoryStatus.EXPIRED:
            expired_rows += 1
        if is_sensitive_memory(row.content):
            sensitive_flagged_rows += 1

    notes = [
        "conversation transcript/thread state is intentionally not migrated; "
        "v2 starts a new session",
        "legacy memory_enabled is reported for cutover but is not applied to v2 runtime "
        "by this tool",
        "sensitive_flagged_rows is a count only; memory contents are never printed by the report",
    ]
    if not chat_ids:
        notes.append(
            "legacy database contained no chat rows; explicit chat_id was accepted for a "
            "zero-record rehearsal"
        )
    return MigrationRehearsalReport(
        source_sha256=source_hash_before,
        source_size_bytes=source_size,
        chat_id=chat_id,
        source_chat_count=len(chat_ids),
        memory_enabled=memory_enabled,
        source_rows=len(rows),
        active_rows=active_rows,
        forgotten_rows=forgotten_rows,
        expired_rows=expired_rows,
        sensitive_flagged_rows=sensitive_flagged_rows,
        kind_counts=kind_counts,
        first_pass=first,
        second_pass=second,
        target_records=target_records,
        source_unchanged=True,
        notes=tuple(notes),
    )
