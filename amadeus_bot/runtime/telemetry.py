from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

PolicyMode = Literal["fast", "llm"]


class RoutingReviewLabel(StrEnum):
    CORRECT = "correct"
    FALSE_FAST = "false_fast"
    FALSE_LLM = "false_llm"
    UNCERTAIN = "uncertain"


class MemoryReviewLabel(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    MEMORY_MISSING = "memory_missing"
    RETRIEVAL_MISS = "retrieval_miss"
    GENERATOR_MISUSE = "generator_misuse"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class TurnTelemetryRecord:
    turn_id: str
    chat_id: int
    observed_at: datetime
    policy_mode: PolicyMode
    policy_act: str
    policy_reason_label: str
    retrieval_ms: int
    policy_ms: int
    context_ms: int
    generation_ms: int
    character_total_ms: int
    time_to_send_ms: int
    finalize_ms: int
    queue_wait_ms: int
    memory_enabled: bool
    retrieved_memory_ids: tuple[str, ...] = ()
    retrospective_triggered: bool = False
    warnings: tuple[str, ...] = ()
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    telegram_message_id: int | None = None
    generator_model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    @property
    def response_wait_ms(self) -> int:
        """Visible wait from router dispatch through successful Telegram send.

        This includes same-chat FIFO queueing plus the existing generation-to-send path. It does not
        include Telegram getUpdates network/polling delay before the router received the update.
        """

        return self.queue_wait_ms + self.time_to_send_ms


@dataclass(frozen=True, slots=True)
class TurnReview:
    turn_id: str
    routing_label: RoutingReviewLabel | None = None
    memory_label: MemoryReviewLabel | None = None
    note: str = ""
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TurnObservation:
    telemetry: TurnTelemetryRecord
    question_text: str | None
    review: TurnReview | None = None


class SQLiteTurnTelemetryStore:
    """Persistent, content-light observation store for delivered v2 turns.

    User/assistant prose stays in the canonical transcript database. Telemetry only retains IDs that
    can be joined back to that transcript for local operator reports.
    """

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._filename = filename
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS v2_turn_telemetry (
              turn_id TEXT PRIMARY KEY,
              chat_id TEXT NOT NULL,
              user_message_id INTEGER,
              assistant_message_id INTEGER,
              telegram_message_id INTEGER,
              observed_at INTEGER NOT NULL,
              policy_mode TEXT NOT NULL CHECK(policy_mode IN ('fast', 'llm')),
              policy_act TEXT NOT NULL,
              policy_reason_label TEXT NOT NULL,
              retrieval_ms INTEGER NOT NULL CHECK(retrieval_ms >= 0),
              policy_ms INTEGER NOT NULL CHECK(policy_ms >= 0),
              context_ms INTEGER NOT NULL CHECK(context_ms >= 0),
              generation_ms INTEGER NOT NULL CHECK(generation_ms >= 0),
              character_total_ms INTEGER NOT NULL CHECK(character_total_ms >= 0),
              time_to_send_ms INTEGER NOT NULL CHECK(time_to_send_ms >= 0),
              finalize_ms INTEGER NOT NULL CHECK(finalize_ms >= 0),
              queue_wait_ms INTEGER NOT NULL CHECK(queue_wait_ms >= 0),
              memory_enabled INTEGER NOT NULL CHECK(memory_enabled IN (0, 1)),
              retrieved_memory_ids_json TEXT NOT NULL,
              retrospective_triggered INTEGER NOT NULL
                CHECK(retrospective_triggered IN (0, 1)),
              warnings_json TEXT NOT NULL,
              generator_model TEXT NOT NULL,
              input_tokens INTEGER,
              output_tokens INTEGER,
              total_tokens INTEGER
            );
            CREATE INDEX IF NOT EXISTS v2_turn_telemetry_recent
              ON v2_turn_telemetry(observed_at DESC);
            CREATE INDEX IF NOT EXISTS v2_turn_telemetry_chat_recent
              ON v2_turn_telemetry(chat_id, observed_at DESC);

            CREATE TABLE IF NOT EXISTS v2_turn_reviews (
              turn_id TEXT PRIMARY KEY,
              routing_label TEXT CHECK(
                routing_label IS NULL OR routing_label IN (
                  'correct', 'false_fast', 'false_llm', 'uncertain'
                )
              ),
              memory_label TEXT CHECK(
                memory_label IS NULL OR memory_label IN (
                  'not_applicable', 'memory_missing', 'retrieval_miss',
                  'generator_misuse', 'uncertain'
                )
              ),
              note TEXT NOT NULL DEFAULT '',
              updated_at INTEGER NOT NULL
            );
            """
        )
        self._db.commit()

    @property
    def filename(self) -> Path:
        return self._filename

    def close(self) -> None:
        self._db.close()

    def record(self, record: TurnTelemetryRecord) -> None:
        self._validate_record(record)
        with self._db:
            self._db.execute(
                """
                INSERT INTO v2_turn_telemetry(
                  turn_id, chat_id, user_message_id, assistant_message_id,
                  telegram_message_id, observed_at, policy_mode, policy_act,
                  policy_reason_label, retrieval_ms, policy_ms, context_ms,
                  generation_ms, character_total_ms, time_to_send_ms,
                  finalize_ms, queue_wait_ms, memory_enabled,
                  retrieved_memory_ids_json, retrospective_triggered,
                  warnings_json, generator_model, input_tokens, output_tokens,
                  total_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO NOTHING
                """,
                (
                    record.turn_id,
                    str(record.chat_id),
                    record.user_message_id,
                    record.assistant_message_id,
                    record.telegram_message_id,
                    int(record.observed_at.timestamp()),
                    record.policy_mode,
                    record.policy_act,
                    record.policy_reason_label,
                    record.retrieval_ms,
                    record.policy_ms,
                    record.context_ms,
                    record.generation_ms,
                    record.character_total_ms,
                    record.time_to_send_ms,
                    record.finalize_ms,
                    record.queue_wait_ms,
                    int(record.memory_enabled),
                    json.dumps(record.retrieved_memory_ids, ensure_ascii=False),
                    int(record.retrospective_triggered),
                    json.dumps(record.warnings, ensure_ascii=False),
                    record.generator_model,
                    record.input_tokens,
                    record.output_tokens,
                    record.total_tokens,
                ),
            )

    def list_since(
        self,
        since: datetime,
        *,
        chat_id: int | None = None,
    ) -> tuple[TurnTelemetryRecord, ...]:
        self._validate_aware(since)
        params: list[object] = [int(since.timestamp())]
        where = "observed_at >= ?"
        if chat_id is not None:
            where += " AND chat_id = ?"
            params.append(str(chat_id))
        rows = self._db.execute(
            f"SELECT * FROM v2_turn_telemetry WHERE {where} ORDER BY observed_at ASC, rowid ASC",
            tuple(params),
        ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    def get(self, turn_id: str) -> TurnTelemetryRecord | None:
        row = self._db.execute(
            "SELECT * FROM v2_turn_telemetry WHERE turn_id = ?",
            (turn_id.strip(),),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def resolve_turn_id(self, prefix: str) -> str:
        normalized = prefix.strip()
        if not normalized:
            raise ValueError("turn id/prefix must not be empty")
        rows = self._db.execute(
            "SELECT turn_id FROM v2_turn_telemetry WHERE turn_id LIKE ? ORDER BY observed_at DESC LIMIT 2",
            (normalized + "%",),
        ).fetchall()
        if not rows:
            raise ValueError(f"no telemetry turn matches prefix: {normalized}")
        if len(rows) > 1:
            raise ValueError(f"telemetry turn prefix is ambiguous: {normalized}")
        return str(rows[0]["turn_id"])

    def get_review(self, turn_id: str) -> TurnReview | None:
        row = self._db.execute(
            "SELECT * FROM v2_turn_reviews WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        return TurnReview(
            turn_id=str(row["turn_id"]),
            routing_label=(
                RoutingReviewLabel(str(row["routing_label"]))
                if row["routing_label"] is not None
                else None
            ),
            memory_label=(
                MemoryReviewLabel(str(row["memory_label"]))
                if row["memory_label"] is not None
                else None
            ),
            note=str(row["note"]),
            updated_at=datetime.fromtimestamp(int(row["updated_at"]), tz=UTC),
        )

    def set_review(
        self,
        turn_id: str,
        *,
        routing_label: RoutingReviewLabel | None = None,
        memory_label: MemoryReviewLabel | None = None,
        note: str | None = None,
        at: datetime | None = None,
    ) -> TurnReview:
        normalized_turn_id = self.resolve_turn_id(turn_id)
        existing = self.get_review(normalized_turn_id)
        next_routing = routing_label if routing_label is not None else (
            existing.routing_label if existing is not None else None
        )
        next_memory = memory_label if memory_label is not None else (
            existing.memory_label if existing is not None else None
        )
        next_note = note.strip() if note is not None else (existing.note if existing is not None else "")
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        with self._db:
            self._db.execute(
                """
                INSERT INTO v2_turn_reviews(turn_id, routing_label, memory_label, note, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                  routing_label = excluded.routing_label,
                  memory_label = excluded.memory_label,
                  note = excluded.note,
                  updated_at = excluded.updated_at
                """,
                (
                    normalized_turn_id,
                    next_routing.value if next_routing is not None else None,
                    next_memory.value if next_memory is not None else None,
                    next_note,
                    int(timestamp.timestamp()),
                ),
            )
        review = self.get_review(normalized_turn_id)
        assert review is not None
        return review

    def observation(
        self,
        record: TurnTelemetryRecord,
        *,
        runtime_db_path: Path,
    ) -> TurnObservation:
        question = self._lookup_question(runtime_db_path, record)
        return TurnObservation(
            telemetry=record,
            question_text=question,
            review=self.get_review(record.turn_id),
        )

    @staticmethod
    def _lookup_question(runtime_db_path: Path, record: TurnTelemetryRecord) -> str | None:
        if not runtime_db_path.exists():
            return None
        uri = f"file:{runtime_db_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            row: sqlite3.Row | None = None
            if record.user_message_id is not None:
                row = connection.execute(
                    "SELECT content FROM conversation_messages WHERE id = ? AND role = 'user'",
                    (record.user_message_id,),
                ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT cm.content
                    FROM v2_turn_metadata tm
                    JOIN conversation_messages cm ON cm.id = tm.user_message_id
                    WHERE tm.turn_id = ? AND cm.role = 'user'
                    """,
                    (record.turn_id,),
                ).fetchone()
            return str(row["content"]) if row is not None else None
        finally:
            connection.close()

    @classmethod
    def _row_to_record(cls, row: sqlite3.Row) -> TurnTelemetryRecord:
        memory_ids = cls._json_string_tuple(str(row["retrieved_memory_ids_json"]))
        warnings = cls._json_string_tuple(str(row["warnings_json"]))
        return TurnTelemetryRecord(
            turn_id=str(row["turn_id"]),
            chat_id=int(row["chat_id"]),
            user_message_id=(int(row["user_message_id"]) if row["user_message_id"] is not None else None),
            assistant_message_id=(
                int(row["assistant_message_id"]) if row["assistant_message_id"] is not None else None
            ),
            telegram_message_id=(
                int(row["telegram_message_id"]) if row["telegram_message_id"] is not None else None
            ),
            observed_at=datetime.fromtimestamp(int(row["observed_at"]), tz=UTC),
            policy_mode=str(row["policy_mode"]),  # type: ignore[arg-type]
            policy_act=str(row["policy_act"]),
            policy_reason_label=str(row["policy_reason_label"]),
            retrieval_ms=int(row["retrieval_ms"]),
            policy_ms=int(row["policy_ms"]),
            context_ms=int(row["context_ms"]),
            generation_ms=int(row["generation_ms"]),
            character_total_ms=int(row["character_total_ms"]),
            time_to_send_ms=int(row["time_to_send_ms"]),
            finalize_ms=int(row["finalize_ms"]),
            queue_wait_ms=int(row["queue_wait_ms"]),
            memory_enabled=bool(row["memory_enabled"]),
            retrieved_memory_ids=memory_ids,
            retrospective_triggered=bool(row["retrospective_triggered"]),
            warnings=warnings,
            generator_model=str(row["generator_model"]),
            input_tokens=(int(row["input_tokens"]) if row["input_tokens"] is not None else None),
            output_tokens=(int(row["output_tokens"]) if row["output_tokens"] is not None else None),
            total_tokens=(int(row["total_tokens"]) if row["total_tokens"] is not None else None),
        )

    @staticmethod
    def _json_string_tuple(raw: str) -> tuple[str, ...]:
        value: object = json.loads(raw)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("telemetry JSON tuple contains invalid data")
        return tuple(value)

    @classmethod
    def _validate_record(cls, record: TurnTelemetryRecord) -> None:
        if not record.turn_id.strip():
            raise ValueError("turn_id must not be empty")
        if record.chat_id <= 0:
            raise ValueError("chat_id must be positive")
        cls._validate_aware(record.observed_at)
        if record.policy_mode not in {"fast", "llm"}:
            raise ValueError("policy_mode must be fast or llm")
        if not record.policy_act.strip():
            raise ValueError("policy_act must not be empty")
        timings = (
            record.retrieval_ms,
            record.policy_ms,
            record.context_ms,
            record.generation_ms,
            record.character_total_ms,
            record.time_to_send_ms,
            record.finalize_ms,
            record.queue_wait_ms,
        )
        if any(value < 0 for value in timings):
            raise ValueError("telemetry timings must not be negative")
        for token_count in (record.input_tokens, record.output_tokens, record.total_tokens):
            if token_count is not None and token_count < 0:
                raise ValueError("telemetry token counts must not be negative")

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("telemetry timestamp must be timezone-aware")
