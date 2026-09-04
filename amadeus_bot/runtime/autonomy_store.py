from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.character import AutonomyAction


@dataclass(frozen=True, slots=True)
class AutonomyDeliveryStats:
    """Persisted delivery statistics consumed by program-owned autonomy guards."""

    last_autonomy_message_at: datetime | None = None
    autonomy_messages_last_24h: int = 0
    consecutive_unanswered_autonomy: int = 0
    sleep_messages_since_start: int = 0


@dataclass(frozen=True, slots=True)
class StoredAutonomyEvaluation:
    evaluation_id: int
    chat_id: int
    generation: int
    action: AutonomyAction
    reason_label: str
    selected_signal_id: str | None
    motivation: float
    evaluated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredAutonomyDelivery:
    delivery_id: int
    chat_id: int
    generation: int
    action: AutonomyAction
    signal_id: str
    delivered_at: datetime
    evaluation_id: int | None = None


class SQLiteAutonomyRuntimeStore:
    """Persistent autonomy opportunity cadence, evaluations and confirmed delivery statistics."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS autonomy_opportunities (
                  chat_id TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  last_opportunity_at INTEGER NOT NULL,
                  PRIMARY KEY(chat_id, generation)
                );
                CREATE TABLE IF NOT EXISTS autonomy_evaluations (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  action TEXT NOT NULL,
                  reason_label TEXT NOT NULL,
                  selected_signal_id TEXT,
                  motivation REAL NOT NULL,
                  evaluated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS autonomy_evaluations_recent
                  ON autonomy_evaluations(chat_id, generation, evaluated_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS autonomy_deliveries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  action TEXT NOT NULL,
                  signal_id TEXT NOT NULL,
                  delivered_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS autonomy_deliveries_recent
                  ON autonomy_deliveries(chat_id, generation, delivered_at DESC, id DESC);
                """
            )
            delivery_columns = {
                str(row["name"])
                for row in self._db.execute("PRAGMA table_info(autonomy_deliveries)").fetchall()
            }
            if "evaluation_id" not in delivery_columns:
                self._db.execute("ALTER TABLE autonomy_deliveries ADD COLUMN evaluation_id INTEGER")
            self._db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS autonomy_deliveries_evaluation
                  ON autonomy_deliveries(evaluation_id)
                  WHERE evaluation_id IS NOT NULL
                """
            )

    def close(self) -> None:
        self._db.close()

    def last_opportunity_at(self, chat_id: int, generation: int) -> datetime | None:
        self._validate_generation(generation)
        row = self._db.execute(
            """
            SELECT last_opportunity_at FROM autonomy_opportunities
            WHERE chat_id = ? AND generation = ?
            """,
            (str(chat_id), generation),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromtimestamp(int(row["last_opportunity_at"]), tz=UTC)

    def mark_opportunity(self, chat_id: int, generation: int, *, at: datetime) -> None:
        self._validate_generation(generation)
        self._validate_aware(at)
        timestamp = int(at.timestamp())
        with self._db:
            self._db.execute(
                """
                INSERT INTO autonomy_opportunities(chat_id, generation, last_opportunity_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, generation) DO UPDATE SET
                  last_opportunity_at = excluded.last_opportunity_at
                """,
                (str(chat_id), generation, timestamp),
            )

    def record_evaluation(
        self,
        chat_id: int,
        generation: int,
        *,
        action: AutonomyAction,
        reason_label: str,
        selected_signal_id: str | None,
        motivation: float,
        at: datetime,
    ) -> StoredAutonomyEvaluation:
        self._validate_generation(generation)
        self._validate_aware(at)
        if not 0.0 <= motivation <= 1.0:
            raise ValueError("autonomy evaluation motivation must be between 0 and 1")
        reason = reason_label.strip() or "unspecified"
        selected = selected_signal_id.strip() if selected_signal_id is not None else None
        if selected == "":
            selected = None
        timestamp = int(at.timestamp())
        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO autonomy_evaluations(
                  chat_id, generation, action, reason_label,
                  selected_signal_id, motivation, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    generation,
                    action.value,
                    reason,
                    selected,
                    motivation,
                    timestamp,
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return autonomy evaluation ID")
        return StoredAutonomyEvaluation(
            evaluation_id=int(cursor.lastrowid),
            chat_id=chat_id,
            generation=generation,
            action=action,
            reason_label=reason,
            selected_signal_id=selected,
            motivation=motivation,
            evaluated_at=datetime.fromtimestamp(timestamp, tz=UTC),
        )

    def last_evaluation(self, chat_id: int, generation: int) -> StoredAutonomyEvaluation | None:
        self._validate_generation(generation)
        row = self._db.execute(
            """
            SELECT id, chat_id, generation, action, reason_label,
                   selected_signal_id, motivation, evaluated_at
            FROM autonomy_evaluations
            WHERE chat_id = ? AND generation = ?
            ORDER BY evaluated_at DESC, id DESC LIMIT 1
            """,
            (str(chat_id), generation),
        ).fetchone()
        return None if row is None else self._evaluation_from_row(row)

    def list_evaluations_since(
        self,
        since: datetime,
        *,
        chat_id: int | None = None,
        limit: int = 5000,
    ) -> tuple[StoredAutonomyEvaluation, ...]:
        self._validate_aware(since)
        self._validate_limit(limit)
        sql = (
            "SELECT id, chat_id, generation, action, reason_label, selected_signal_id, "
            "motivation, evaluated_at FROM autonomy_evaluations WHERE evaluated_at >= ?"
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

    def record_delivery(
        self,
        chat_id: int,
        generation: int,
        *,
        action: AutonomyAction,
        signal_id: str,
        at: datetime,
        evaluation_id: int | None = None,
    ) -> StoredAutonomyDelivery:
        self._validate_generation(generation)
        self._validate_aware(at)
        if action is AutonomyAction.SILENT:
            raise ValueError("SILENT autonomy decisions cannot be recorded as deliveries")
        normalized_signal = signal_id.strip()
        if not normalized_signal:
            raise ValueError("autonomy delivery requires signal_id")
        if evaluation_id is not None and evaluation_id <= 0:
            raise ValueError("autonomy evaluation_id must be positive")
        timestamp = int(at.timestamp())
        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO autonomy_deliveries(
                  chat_id, generation, action, signal_id, delivered_at, evaluation_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    generation,
                    action.value,
                    normalized_signal,
                    timestamp,
                    evaluation_id,
                ),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return autonomy delivery ID")
        return StoredAutonomyDelivery(
            delivery_id=int(cursor.lastrowid),
            chat_id=chat_id,
            generation=generation,
            action=action,
            signal_id=normalized_signal,
            delivered_at=datetime.fromtimestamp(timestamp, tz=UTC),
            evaluation_id=evaluation_id,
        )

    def list_deliveries_since(
        self,
        since: datetime,
        *,
        chat_id: int | None = None,
        limit: int = 5000,
    ) -> tuple[StoredAutonomyDelivery, ...]:
        self._validate_aware(since)
        self._validate_limit(limit)
        sql = (
            "SELECT id, chat_id, generation, action, signal_id, delivered_at, evaluation_id "
            "FROM autonomy_deliveries WHERE delivered_at >= ?"
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

    def delivery_stats(
        self,
        chat_id: int,
        generation: int,
        *,
        now: datetime,
        last_user_message_at: datetime | None,
        sleep_started_at: datetime | None = None,
    ) -> AutonomyDeliveryStats:
        self._validate_generation(generation)
        self._validate_aware(now)
        if last_user_message_at is not None:
            self._validate_aware(last_user_message_at)
            if last_user_message_at > now:
                raise ValueError("last_user_message_at must not be in the future")
        if sleep_started_at is not None:
            self._validate_aware(sleep_started_at)
            if sleep_started_at > now:
                raise ValueError("sleep_started_at must not be in the future")

        latest = self._db.execute(
            """
            SELECT delivered_at FROM autonomy_deliveries
            WHERE chat_id = ? AND generation = ?
            ORDER BY delivered_at DESC, id DESC LIMIT 1
            """,
            (str(chat_id), generation),
        ).fetchone()
        last_autonomy = (
            None
            if latest is None
            else datetime.fromtimestamp(int(latest["delivered_at"]), tz=UTC)
        )

        since_24h = int((now - timedelta(hours=24)).timestamp())
        count_row = self._db.execute(
            """
            SELECT COUNT(*) AS count FROM autonomy_deliveries
            WHERE chat_id = ? AND generation = ? AND delivered_at >= ?
            """,
            (str(chat_id), generation, since_24h),
        ).fetchone()
        count_24h = int(count_row["count"]) if count_row is not None else 0

        if last_user_message_at is None:
            unanswered = 0
        else:
            user_timestamp = int(last_user_message_at.timestamp())
            unanswered_row = self._db.execute(
                """
                SELECT COUNT(*) AS count FROM autonomy_deliveries
                WHERE chat_id = ? AND generation = ? AND delivered_at > ?
                """,
                (str(chat_id), generation, user_timestamp),
            ).fetchone()
            unanswered = int(unanswered_row["count"]) if unanswered_row is not None else 0

        sleep_count = 0
        if sleep_started_at is not None:
            sleep_row = self._db.execute(
                """
                SELECT COUNT(*) AS count FROM autonomy_deliveries
                WHERE chat_id = ? AND generation = ? AND delivered_at >= ?
                """,
                (str(chat_id), generation, int(sleep_started_at.timestamp())),
            ).fetchone()
            sleep_count = int(sleep_row["count"]) if sleep_row is not None else 0

        return AutonomyDeliveryStats(
            last_autonomy_message_at=last_autonomy,
            autonomy_messages_last_24h=count_24h,
            consecutive_unanswered_autonomy=unanswered,
            sleep_messages_since_start=sleep_count,
        )

    @staticmethod
    def _evaluation_from_row(row: sqlite3.Row) -> StoredAutonomyEvaluation:
        return StoredAutonomyEvaluation(
            evaluation_id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            generation=int(row["generation"]),
            action=AutonomyAction(str(row["action"])),
            reason_label=str(row["reason_label"]),
            selected_signal_id=(
                str(row["selected_signal_id"]) if row["selected_signal_id"] is not None else None
            ),
            motivation=float(row["motivation"]),
            evaluated_at=datetime.fromtimestamp(int(row["evaluated_at"]), tz=UTC),
        )

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> StoredAutonomyDelivery:
        return StoredAutonomyDelivery(
            delivery_id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            generation=int(row["generation"]),
            action=AutonomyAction(str(row["action"])),
            signal_id=str(row["signal_id"]),
            delivered_at=datetime.fromtimestamp(int(row["delivered_at"]), tz=UTC),
            evaluation_id=int(row["evaluation_id"]) if row["evaluation_id"] is not None else None,
        )

    @staticmethod
    def _validate_generation(generation: int) -> None:
        if generation < 1:
            raise ValueError("conversation generation must be >= 1")

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("autonomy runtime timestamp must be timezone-aware")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < 1 or limit > 10000:
            raise ValueError("autonomy history limit must be between 1 and 10000")
