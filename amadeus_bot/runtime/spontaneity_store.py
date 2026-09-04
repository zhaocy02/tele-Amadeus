from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.character.spontaneity import SpontaneityAction


@dataclass(frozen=True, slots=True)
class SpontaneityDeliveryStats:
    last_delivery_at: datetime | None = None
    messages_last_24h: int = 0


@dataclass(frozen=True, slots=True)
class StoredSpontaneityEvaluation:
    evaluation_id: int
    chat_id: int
    generation: int
    source_turn_id: str
    action: SpontaneityAction
    reason_label: str
    motivation: float
    delay_seconds: float
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredSpontaneityDelivery:
    delivery_id: int
    chat_id: int
    generation: int
    source_turn_id: str
    delivered_at: datetime
    evaluation_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoredSpontaneityEpisodeMessage:
    episode_message_id: int
    chat_id: int
    generation: int
    episode_id: str
    source_turn_id: str
    sequence_index: int
    delivery_source_turn_id: str
    evaluation_id: int | None
    delivery_id: int | None
    recorded_at: datetime


class SQLiteSpontaneityStore:
    """Durable observation, counters, and episode lineage for short-horizon continuations."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS spontaneity_evaluations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  source_turn_id TEXT NOT NULL UNIQUE,
                  action TEXT NOT NULL,
                  reason_label TEXT NOT NULL,
                  motivation REAL NOT NULL,
                  delay_seconds REAL NOT NULL,
                  evaluated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS spontaneity_evaluations_recent
                  ON spontaneity_evaluations(chat_id, generation, evaluated_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS spontaneity_deliveries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  source_turn_id TEXT NOT NULL UNIQUE,
                  evaluation_id INTEGER,
                  delivered_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS spontaneity_deliveries_recent
                  ON spontaneity_deliveries(chat_id, generation, delivered_at DESC, id DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS spontaneity_deliveries_evaluation
                  ON spontaneity_deliveries(evaluation_id)
                  WHERE evaluation_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS spontaneity_episode_messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  episode_id TEXT NOT NULL,
                  source_turn_id TEXT NOT NULL,
                  sequence_index INTEGER NOT NULL CHECK(sequence_index >= 1),
                  delivery_source_turn_id TEXT NOT NULL UNIQUE,
                  evaluation_id INTEGER,
                  delivery_id INTEGER,
                  recorded_at INTEGER NOT NULL,
                  UNIQUE(episode_id, sequence_index)
                );
                CREATE INDEX IF NOT EXISTS spontaneity_episode_messages_recent
                  ON spontaneity_episode_messages(chat_id, generation, recorded_at DESC, id DESC);
                """
            )

    def close(self) -> None:
        self._db.close()

    def record_evaluation(
        self,
        chat_id: int,
        generation: int,
        *,
        source_turn_id: str,
        action: SpontaneityAction,
        reason_label: str,
        motivation: float,
        delay_seconds: float,
        at: datetime,
    ) -> StoredSpontaneityEvaluation:
        self._validate_chat_generation(chat_id, generation)
        self._validate_aware(at)
        source = source_turn_id.strip()
        if not source:
            raise ValueError("spontaneity source_turn_id must not be empty")
        if not 0.0 <= motivation <= 1.0:
            raise ValueError("spontaneity motivation must be between 0 and 1")
        if delay_seconds < 0:
            raise ValueError("spontaneity delay_seconds must not be negative")
        reason = reason_label.strip() or "unspecified"
        timestamp = int(at.timestamp())

        existing = self._db.execute(
            """
            SELECT id, chat_id, generation, source_turn_id, action, reason_label,
                   motivation, delay_seconds, evaluated_at
            FROM spontaneity_evaluations WHERE source_turn_id = ?
            """,
            (source,),
        ).fetchone()
        if existing is not None:
            return self._evaluation_from_row(existing)

        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO spontaneity_evaluations(
                  chat_id, generation, source_turn_id, action, reason_label,
                  motivation, delay_seconds, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    generation,
                    source,
                    action.value,
                    reason,
                    motivation,
                    delay_seconds,
                    timestamp,
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return spontaneity evaluation ID")
        return StoredSpontaneityEvaluation(
            evaluation_id=int(cursor.lastrowid),
            chat_id=chat_id,
            generation=generation,
            source_turn_id=source,
            action=action,
            reason_label=reason,
            motivation=motivation,
            delay_seconds=delay_seconds,
            evaluated_at=datetime.fromtimestamp(timestamp, tz=UTC),
        )

    def record_delivery(
        self,
        chat_id: int,
        generation: int,
        *,
        source_turn_id: str,
        at: datetime,
        evaluation_id: int | None = None,
    ) -> StoredSpontaneityDelivery:
        self._validate_chat_generation(chat_id, generation)
        self._validate_aware(at)
        source = source_turn_id.strip()
        if not source:
            raise ValueError("spontaneity delivery requires source_turn_id")
        if evaluation_id is not None and evaluation_id <= 0:
            raise ValueError("spontaneity evaluation_id must be positive")

        existing = self._db.execute(
            """
            SELECT id, chat_id, generation, source_turn_id, evaluation_id, delivered_at
            FROM spontaneity_deliveries WHERE source_turn_id = ?
            """,
            (source,),
        ).fetchone()
        if existing is not None:
            return self._delivery_from_row(existing)

        timestamp = int(at.timestamp())
        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO spontaneity_deliveries(
                  chat_id, generation, source_turn_id, evaluation_id, delivered_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (str(chat_id), generation, source, evaluation_id, timestamp),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return spontaneity delivery ID")
        return StoredSpontaneityDelivery(
            delivery_id=int(cursor.lastrowid),
            chat_id=chat_id,
            generation=generation,
            source_turn_id=source,
            delivered_at=datetime.fromtimestamp(timestamp, tz=UTC),
            evaluation_id=evaluation_id,
        )

    def record_episode_message(
        self,
        chat_id: int,
        generation: int,
        *,
        episode_id: str,
        source_turn_id: str,
        sequence_index: int,
        delivery_source_turn_id: str,
        at: datetime,
        evaluation_id: int | None = None,
        delivery_id: int | None = None,
    ) -> StoredSpontaneityEpisodeMessage:
        self._validate_chat_generation(chat_id, generation)
        self._validate_aware(at)
        episode = episode_id.strip()
        source = source_turn_id.strip()
        delivery_source = delivery_source_turn_id.strip()
        if not episode or not source or not delivery_source:
            raise ValueError("spontaneity episode identifiers must not be empty")
        if sequence_index < 1:
            raise ValueError("spontaneity sequence_index must be positive")
        if evaluation_id is not None and evaluation_id <= 0:
            raise ValueError("spontaneity evaluation_id must be positive")
        if delivery_id is not None and delivery_id <= 0:
            raise ValueError("spontaneity delivery_id must be positive")

        existing = self._db.execute(
            """
            SELECT id, chat_id, generation, episode_id, source_turn_id, sequence_index,
                   delivery_source_turn_id, evaluation_id, delivery_id, recorded_at
            FROM spontaneity_episode_messages
            WHERE episode_id = ? AND sequence_index = ?
            """,
            (episode, sequence_index),
        ).fetchone()
        if existing is not None:
            return self._episode_message_from_row(existing)

        timestamp = int(at.timestamp())
        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO spontaneity_episode_messages(
                  chat_id, generation, episode_id, source_turn_id, sequence_index,
                  delivery_source_turn_id, evaluation_id, delivery_id, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    generation,
                    episode,
                    source,
                    sequence_index,
                    delivery_source,
                    evaluation_id,
                    delivery_id,
                    timestamp,
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return spontaneity episode-message ID")
        return StoredSpontaneityEpisodeMessage(
            episode_message_id=int(cursor.lastrowid),
            chat_id=chat_id,
            generation=generation,
            episode_id=episode,
            source_turn_id=source,
            sequence_index=sequence_index,
            delivery_source_turn_id=delivery_source,
            evaluation_id=evaluation_id,
            delivery_id=delivery_id,
            recorded_at=datetime.fromtimestamp(timestamp, tz=UTC),
        )

    def delivery_stats(
        self,
        chat_id: int,
        generation: int,
        *,
        now: datetime,
    ) -> SpontaneityDeliveryStats:
        self._validate_chat_generation(chat_id, generation)
        self._validate_aware(now)
        latest = self._db.execute(
            """
            SELECT delivered_at FROM spontaneity_deliveries
            WHERE chat_id = ? AND generation = ?
            ORDER BY delivered_at DESC, id DESC LIMIT 1
            """,
            (str(chat_id), generation),
        ).fetchone()
        last_delivery = (
            None
            if latest is None
            else datetime.fromtimestamp(int(latest["delivered_at"]), tz=UTC)
        )
        since_24h = int((now - timedelta(hours=24)).timestamp())
        row = self._db.execute(
            """
            SELECT COUNT(*) AS count FROM spontaneity_deliveries
            WHERE chat_id = ? AND generation = ? AND delivered_at >= ?
            """,
            (str(chat_id), generation, since_24h),
        ).fetchone()
        count = int(row["count"]) if row is not None else 0
        return SpontaneityDeliveryStats(
            last_delivery_at=last_delivery,
            messages_last_24h=count,
        )

    def list_evaluations_since(
        self,
        since: datetime,
        *,
        chat_id: int | None = None,
        limit: int = 5000,
    ) -> tuple[StoredSpontaneityEvaluation, ...]:
        self._validate_aware(since)
        self._validate_limit(limit)
        sql = (
            "SELECT id, chat_id, generation, source_turn_id, action, reason_label, "
            "motivation, delay_seconds, evaluated_at FROM spontaneity_evaluations "
            "WHERE evaluated_at >= ?"
        )
        params: list[object] = [int(since.timestamp())]
        if chat_id is not None:
            if chat_id <= 0:
                raise ValueError("chat_id must be positive")
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        sql += " ORDER BY evaluated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(sql, tuple(params)).fetchall()
        return tuple(self._evaluation_from_row(row) for row in rows)

    def list_deliveries_since(
        self,
        since: datetime,
        *,
        chat_id: int | None = None,
        limit: int = 5000,
    ) -> tuple[StoredSpontaneityDelivery, ...]:
        self._validate_aware(since)
        self._validate_limit(limit)
        sql = (
            "SELECT id, chat_id, generation, source_turn_id, evaluation_id, delivered_at "
            "FROM spontaneity_deliveries WHERE delivered_at >= ?"
        )
        params: list[object] = [int(since.timestamp())]
        if chat_id is not None:
            if chat_id <= 0:
                raise ValueError("chat_id must be positive")
            sql += " AND chat_id = ?"
            params.append(str(chat_id))
        sql += " ORDER BY delivered_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(sql, tuple(params)).fetchall()
        return tuple(self._delivery_from_row(row) for row in rows)

    def list_episode_messages(
        self,
        episode_id: str,
    ) -> tuple[StoredSpontaneityEpisodeMessage, ...]:
        episode = episode_id.strip()
        if not episode:
            raise ValueError("spontaneity episode_id must not be empty")
        rows = self._db.execute(
            """
            SELECT id, chat_id, generation, episode_id, source_turn_id, sequence_index,
                   delivery_source_turn_id, evaluation_id, delivery_id, recorded_at
            FROM spontaneity_episode_messages
            WHERE episode_id = ? ORDER BY sequence_index ASC, id ASC
            """,
            (episode,),
        ).fetchall()
        return tuple(self._episode_message_from_row(row) for row in rows)

    @staticmethod
    def _evaluation_from_row(row: sqlite3.Row) -> StoredSpontaneityEvaluation:
        return StoredSpontaneityEvaluation(
            evaluation_id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            generation=int(row["generation"]),
            source_turn_id=str(row["source_turn_id"]),
            action=SpontaneityAction(str(row["action"])),
            reason_label=str(row["reason_label"]),
            motivation=float(row["motivation"]),
            delay_seconds=float(row["delay_seconds"]),
            evaluated_at=datetime.fromtimestamp(int(row["evaluated_at"]), tz=UTC),
        )

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> StoredSpontaneityDelivery:
        return StoredSpontaneityDelivery(
            delivery_id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            generation=int(row["generation"]),
            source_turn_id=str(row["source_turn_id"]),
            delivered_at=datetime.fromtimestamp(int(row["delivered_at"]), tz=UTC),
            evaluation_id=(
                None if row["evaluation_id"] is None else int(row["evaluation_id"])
            ),
        )

    @staticmethod
    def _episode_message_from_row(row: sqlite3.Row) -> StoredSpontaneityEpisodeMessage:
        return StoredSpontaneityEpisodeMessage(
            episode_message_id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            generation=int(row["generation"]),
            episode_id=str(row["episode_id"]),
            source_turn_id=str(row["source_turn_id"]),
            sequence_index=int(row["sequence_index"]),
            delivery_source_turn_id=str(row["delivery_source_turn_id"]),
            evaluation_id=(
                None if row["evaluation_id"] is None else int(row["evaluation_id"])
            ),
            delivery_id=None if row["delivery_id"] is None else int(row["delivery_id"]),
            recorded_at=datetime.fromtimestamp(int(row["recorded_at"]), tz=UTC),
        )

    @staticmethod
    def _validate_chat_generation(chat_id: int, generation: int) -> None:
        if chat_id <= 0:
            raise ValueError("chat_id must be positive")
        if generation < 1:
            raise ValueError("generation must be positive")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < 1 or limit > 10000:
            raise ValueError("limit must be between 1 and 10000")

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("spontaneity timestamp must be timezone-aware")
