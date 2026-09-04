from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.llm import MessageRole
from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    StructuredMemoryRepository,
)

from .session_store import ConversationSessionStore

_SENDER_ID_RE = re.compile(r"^(?:user)?(\d+)$")


@dataclass(frozen=True, slots=True)
class TelegramContinuityMessage:
    telegram_message_id: int
    sender_id: int
    role: MessageRole
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ContinuityImportPass:
    created: int
    unchanged: int
    verified: int


@dataclass(frozen=True, slots=True)
class ContinuityImportReport:
    export_sha256: str
    selected_digest: str
    since: str
    source_messages: int
    user_messages: int
    assistant_messages: int
    first_message_at: str
    last_message_at: str
    inferred_bot_sender_id: int
    first_pass: ContinuityImportPass
    second_pass: ContinuityImportPass
    target_messages: int
    episode_memories: int
    relationship_marker_written: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True, slots=True)
class _ImportedMessage:
    source: TelegramContinuityMessage
    conversation_message_id: int


async def import_telegram_continuity(
    *,
    export_path: Path,
    target_data_dir: Path,
    chat_id: int,
    since: datetime,
) -> ContinuityImportReport:
    """Import one private Telegram chat export into an existing/new v2 data directory.

    The export is the canonical transcript source. This function never contacts Telegram or the
    provider. Re-running the same import is idempotent; conflicting pre-existing transcript data
    fails closed.
    """

    if chat_id <= 0:
        raise ValueError("chat_id must be positive")
    _validate_aware(since)
    source = export_path.expanduser().resolve()
    target = target_data_dir.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Telegram export is not a file: {source}")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)

    export_bytes = source.read_bytes()
    export_sha256 = hashlib.sha256(export_bytes).hexdigest()
    parsed = json.loads(export_bytes.decode("utf-8"))
    messages, bot_sender_id = _selected_messages(parsed, user_id=chat_id, since=since)
    if not messages:
        raise RuntimeError("Telegram export contains no selected text messages at/after cutoff")

    runtime_path = target / "runtime.sqlite"
    store = ConversationSessionStore(runtime_path)
    try:
        generation = store.current_generation(chat_id)
    finally:
        store.close()

    first_pass, imported = _import_pass(
        runtime_path=runtime_path,
        chat_id=chat_id,
        generation=generation,
        messages=messages,
    )
    second_pass, second_imported = _import_pass(
        runtime_path=runtime_path,
        chat_id=chat_id,
        generation=generation,
        messages=messages,
    )
    if tuple(item.conversation_message_id for item in imported) != tuple(
        item.conversation_message_id for item in second_imported
    ):
        raise RuntimeError("continuity import idempotency verification changed message mapping")
    if second_pass.created != 0 or second_pass.unchanged != len(messages):
        raise RuntimeError("continuity import second pass was not idempotent")

    target_messages = _target_import_count(runtime_path, chat_id, generation)
    if target_messages != len(messages):
        raise RuntimeError("continuity import target message count mismatch")

    memory = StructuredMemoryRepository(target / "structured-memory.sqlite")
    try:
        episode_count = await _write_episode_memories(memory, imported, since=since)
        marker_written = await _write_relationship_marker(
            memory,
            imported,
            since=since,
        )
    finally:
        memory.close()

    for path in (runtime_path, target / "structured-memory.sqlite"):
        if path.exists():
            path.chmod(0o600)

    selected_digest = _selected_digest(messages)
    return ContinuityImportReport(
        export_sha256=export_sha256,
        selected_digest=selected_digest,
        since=since.isoformat(),
        source_messages=len(messages),
        user_messages=sum(item.role is MessageRole.USER for item in messages),
        assistant_messages=sum(item.role is MessageRole.ASSISTANT for item in messages),
        first_message_at=messages[0].created_at.isoformat(),
        last_message_at=messages[-1].created_at.isoformat(),
        inferred_bot_sender_id=bot_sender_id,
        first_pass=first_pass,
        second_pass=second_pass,
        target_messages=target_messages,
        episode_memories=episode_count,
        relationship_marker_written=marker_written,
    )


def _selected_messages(
    document: object,
    *,
    user_id: int,
    since: datetime,
) -> tuple[tuple[TelegramContinuityMessage, ...], int]:
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        raise ValueError("Telegram Desktop export must contain a top-level messages array")

    candidates: list[tuple[int, int, str, datetime]] = []
    non_user_senders: set[int] = set()
    seen_ids: set[int] = set()
    for raw in document["messages"]:
        if not isinstance(raw, dict) or raw.get("type") != "message":
            continue
        message_id = raw.get("id")
        if not isinstance(message_id, int) or message_id <= 0:
            continue
        sender_id = _parse_sender_id(raw.get("from_id"))
        if sender_id is None:
            continue
        created_at = _parse_message_datetime(raw)
        if created_at < since.astimezone(UTC):
            continue
        content = _flatten_text(raw.get("text"))
        if not content.strip():
            continue
        if message_id in seen_ids:
            raise RuntimeError(f"duplicate Telegram message id in export: {message_id}")
        seen_ids.add(message_id)
        candidates.append((message_id, sender_id, content, created_at))
        if sender_id != user_id:
            non_user_senders.add(sender_id)

    if len(non_user_senders) != 1:
        raise RuntimeError(
            "selected private-chat export must contain exactly one non-user sender after cutoff"
        )
    bot_sender_id = next(iter(non_user_senders))
    messages = tuple(
        TelegramContinuityMessage(
            telegram_message_id=message_id,
            sender_id=sender_id,
            role=MessageRole.USER if sender_id == user_id else MessageRole.ASSISTANT,
            content=content,
            created_at=created_at,
        )
        for message_id, sender_id, content, created_at in sorted(
            candidates,
            key=lambda item: (item[3].timestamp(), item[0]),
        )
        if sender_id in {user_id, bot_sender_id}
    )
    return messages, bot_sender_id


def _import_pass(
    *,
    runtime_path: Path,
    chat_id: int,
    generation: int,
    messages: tuple[TelegramContinuityMessage, ...],
) -> tuple[ContinuityImportPass, tuple[_ImportedMessage, ...]]:
    db = sqlite3.connect(runtime_path)
    db.row_factory = sqlite3.Row
    try:
        _create_provenance_schema(db)
        _assert_no_unmanaged_transcript(db, chat_id, generation)
        created = 0
        unchanged = 0
        imported: list[_ImportedMessage] = []
        with db:
            for message in messages:
                digest = _message_digest(message)
                existing = db.execute(
                    """
                    SELECT p.conversation_message_id, p.source_digest,
                           m.role, m.content, m.created_at
                    FROM legacy_telegram_message_provenance AS p
                    JOIN conversation_messages AS m ON m.id = p.conversation_message_id
                    WHERE p.chat_id = ? AND p.telegram_message_id = ?
                    """,
                    (str(chat_id), message.telegram_message_id),
                ).fetchone()
                if existing is not None:
                    _verify_existing(existing, message, digest)
                    unchanged += 1
                    imported.append(
                        _ImportedMessage(message, int(existing["conversation_message_id"]))
                    )
                    continue

                cursor = db.execute(
                    """
                    INSERT INTO conversation_messages(
                        chat_id, generation, role, content, created_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(chat_id),
                        generation,
                        message.role.value,
                        message.content,
                        int(message.created_at.timestamp()),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return imported conversation message id")
                conversation_message_id = int(cursor.lastrowid)
                db.execute(
                    """
                    INSERT INTO legacy_telegram_message_provenance(
                        chat_id, telegram_message_id, generation, conversation_message_id,
                        sender_id, role, source_timestamp, source_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(chat_id),
                        message.telegram_message_id,
                        generation,
                        conversation_message_id,
                        message.sender_id,
                        message.role.value,
                        int(message.created_at.timestamp()),
                        digest,
                    ),
                )
                imported.append(_ImportedMessage(message, conversation_message_id))
                created += 1
            db.execute(
                "UPDATE conversation_sessions SET updated_at = ? WHERE chat_id = ?",
                (int(messages[-1].created_at.timestamp()), str(chat_id)),
            )
        return (
            ContinuityImportPass(created=created, unchanged=unchanged, verified=len(messages)),
            tuple(imported),
        )
    finally:
        db.close()


def _create_provenance_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS legacy_telegram_message_provenance (
          chat_id TEXT NOT NULL,
          telegram_message_id INTEGER NOT NULL,
          generation INTEGER NOT NULL,
          conversation_message_id INTEGER NOT NULL UNIQUE,
          sender_id INTEGER NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
          source_timestamp INTEGER NOT NULL,
          source_digest TEXT NOT NULL,
          PRIMARY KEY(chat_id, telegram_message_id)
        )
        """
    )
    db.commit()


def _assert_no_unmanaged_transcript(
    db: sqlite3.Connection,
    chat_id: int,
    generation: int,
) -> None:
    row = db.execute(
        """
        SELECT COUNT(*) AS count
        FROM conversation_messages AS m
        LEFT JOIN legacy_telegram_message_provenance AS p
          ON p.conversation_message_id = m.id
        WHERE m.chat_id = ? AND m.generation = ? AND p.conversation_message_id IS NULL
        """,
        (str(chat_id), generation),
    ).fetchone()
    if row is not None and int(row["count"]) != 0:
        raise RuntimeError(
            "continuity target already contains non-imported conversation transcript"
        )


def _verify_existing(row: sqlite3.Row, message: TelegramContinuityMessage, digest: str) -> None:
    expected_timestamp = int(message.created_at.timestamp())
    if (
        str(row["source_digest"]) != digest
        or str(row["role"]) != message.role.value
        or str(row["content"]) != message.content
        or int(row["created_at"]) != expected_timestamp
    ):
        raise RuntimeError(
            f"conflicting continuity data for Telegram message {message.telegram_message_id}"
        )


def _target_import_count(runtime_path: Path, chat_id: int, generation: int) -> int:
    db = sqlite3.connect(runtime_path)
    try:
        row = db.execute(
            """
            SELECT COUNT(*) FROM legacy_telegram_message_provenance
            WHERE chat_id = ? AND generation = ?
            """,
            (str(chat_id), generation),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        db.close()


async def _write_episode_memories(
    repository: StructuredMemoryRepository,
    imported: tuple[_ImportedMessage, ...],
    *,
    since: datetime,
) -> int:
    chunks = _episode_chunks(imported)
    for chunk in chunks:
        content = _render_episode_chunk(chunk)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
        memory = MemoryRecord(
            memory_id=f"legacy_tg_episode_{digest}",
            kind=MemoryKind.EPISODE,
            content=content,
            confidence=1.0,
            salience=0.96,
            created_at=chunk[0].source.created_at,
            updated_at=chunk[-1].source.created_at,
            source_message_ids=tuple(item.conversation_message_id for item in chunk),
            source_type=MemorySourceType.MIGRATED,
            tags=("legacy_v1", "telegram_export", "continuity", f"since:{since.isoformat()}"),
        )
        existing = await repository.get(memory.memory_id)
        if existing is None:
            await repository.upsert(memory)
        elif existing != memory:
            raise RuntimeError(f"conflicting continuity episode memory: {memory.memory_id}")
    return len(chunks)


async def _write_relationship_marker(
    repository: StructuredMemoryRepository,
    imported: tuple[_ImportedMessage, ...],
    *,
    since: datetime,
) -> bool:
    first = imported[0].source.created_at
    last = imported[-1].source.created_at
    content = (
        "这是从 Amadeus v1 连续继承的真实对话经历：用户与 Amadeus 在 "
        f"{first.isoformat()} 至 {last.isoformat()} 之间有实际交流；切换到 v2 不代表初次见面"
        "或关系重置。该时期的原始对话已作为 transcript 与 continuity episode 保留。"
    )
    selected = _selected_digest(tuple(item.source for item in imported))
    memory = MemoryRecord(
        memory_id=f"legacy_tg_relationship_{selected[:24]}",
        kind=MemoryKind.RELATIONSHIP,
        content=content,
        confidence=1.0,
        salience=0.99,
        created_at=first,
        updated_at=last,
        source_message_ids=tuple(item.conversation_message_id for item in imported),
        source_type=MemorySourceType.MIGRATED,
        tags=("legacy_v1", "telegram_export", "continuity", f"since:{since.isoformat()}"),
    )
    existing = await repository.get(memory.memory_id)
    if existing is None:
        await repository.upsert(memory)
        return True
    if existing != memory:
        raise RuntimeError("conflicting continuity relationship marker")
    return False


def _episode_chunks(
    imported: tuple[_ImportedMessage, ...],
    *,
    max_chars: int = 2800,
) -> tuple[tuple[_ImportedMessage, ...], ...]:
    chunks: list[tuple[_ImportedMessage, ...]] = []
    current: list[_ImportedMessage] = []
    current_chars = 0
    for item in imported:
        rendered = _render_line(item)
        if current and current_chars + len(rendered) + 1 > max_chars:
            chunks.append(tuple(current))
            current = []
            current_chars = 0
        current.append(item)
        current_chars += len(rendered) + 1
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _render_episode_chunk(chunk: tuple[_ImportedMessage, ...]) -> str:
    return "\n".join(_render_line(item) for item in chunk)


def _render_line(item: _ImportedMessage) -> str:
    label = "用户" if item.source.role is MessageRole.USER else "Amadeus"
    return f"[{item.source.created_at.isoformat()}] {label}: {item.source.content}"


def _selected_digest(messages: tuple[TelegramContinuityMessage, ...]) -> str:
    payload = "\n".join(_message_digest(message) for message in messages)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _message_digest(message: TelegramContinuityMessage) -> str:
    payload = json.dumps(
        {
            "telegram_message_id": message.telegram_message_id,
            "sender_id": message.sender_id,
            "role": message.role.value,
            "content": message.content,
            "created_at": int(message.created_at.timestamp()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_sender_id(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _SENDER_ID_RE.fullmatch(value.strip())
    return int(match.group(1)) if match is not None else None


def _parse_message_datetime(raw: dict[str, object]) -> datetime:
    unix_value = raw.get("date_unixtime")
    if isinstance(unix_value, (str, int)) and str(unix_value).isdigit():
        return datetime.fromtimestamp(int(unix_value), tz=UTC)
    date_value = raw.get("date")
    if not isinstance(date_value, str):
        raise ValueError("Telegram message is missing date/date_unixtime")
    parsed = datetime.fromisoformat(date_value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Telegram export message date must include timezone information")
    return parsed.astimezone(UTC)


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(str(item["text"]))
    return "".join(parts)


def _validate_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("continuity cutoff must be timezone-aware")
