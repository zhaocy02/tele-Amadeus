from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.character import (
    AutonomyGuard,
    AutonomyGuardConfig,
    AutonomyGuardResult,
    AutonomyOpportunity,
    AutonomyOpportunityScheduler,
    AutonomyScheduleConfig,
)


@dataclass(frozen=True, slots=True)
class AutonomyTuning:
    """Persistent per-chat tuning knobs that are safe to apply without a process restart."""

    min_user_idle: timedelta = timedelta(minutes=3)
    check_interval: timedelta = timedelta(minutes=5)
    max_messages_per_24h: int = 24
    idle_drive_max_motivation_bonus: float = 0.25
    spontaneity_max_messages_per_24h: int | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not timedelta(minutes=1) <= self.min_user_idle <= timedelta(days=30):
            raise ValueError("autonomy tuning idle must be between 1 minute and 30 days")
        if not timedelta(minutes=1) <= self.check_interval <= timedelta(days=7):
            raise ValueError("autonomy tuning interval must be between 1 minute and 7 days")
        if not 1 <= self.max_messages_per_24h <= 48:
            raise ValueError("autonomy tuning daily cap must be between 1 and 48")
        if not 0.0 <= self.idle_drive_max_motivation_bonus <= 0.5:
            raise ValueError("autonomy tuning drive must be between 0 and 0.5")
        if (
            self.spontaneity_max_messages_per_24h is not None
            and not 1 <= self.spontaneity_max_messages_per_24h <= 720
        ):
            raise ValueError("spontaneity daily cap must be between 1 and 720, or unlimited")
        if self.updated_at is not None and (
            self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None
        ):
            raise ValueError("autonomy tuning updated_at must be timezone-aware")

    @property
    def customized(self) -> bool:
        return self.updated_at is not None


class SQLiteAutonomyTuningStore:
    """Small persistent tuning table colocated with other runtime preferences."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS autonomy_tuning (
              chat_id TEXT PRIMARY KEY,
              min_user_idle_seconds INTEGER NOT NULL DEFAULT 180
                CHECK(min_user_idle_seconds >= 60 AND min_user_idle_seconds <= 2592000),
              check_interval_seconds INTEGER NOT NULL DEFAULT 300
                CHECK(check_interval_seconds >= 60 AND check_interval_seconds <= 604800),
              max_messages_per_24h INTEGER NOT NULL DEFAULT 24
                CHECK(max_messages_per_24h >= 1 AND max_messages_per_24h <= 48),
              idle_drive_max_motivation_bonus REAL NOT NULL DEFAULT 0.25
                CHECK(idle_drive_max_motivation_bonus >= 0.0
                      AND idle_drive_max_motivation_bonus <= 0.5),
              spontaneity_max_messages_per_24h INTEGER
                CHECK(spontaneity_max_messages_per_24h IS NULL
                      OR (spontaneity_max_messages_per_24h >= 1
                          AND spontaneity_max_messages_per_24h <= 720)),
              updated_at INTEGER NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get(self, chat_id: int) -> AutonomyTuning:
        self._validate_chat_id(chat_id)
        row = self._db.execute(
            """
            SELECT min_user_idle_seconds, check_interval_seconds,
                   max_messages_per_24h, idle_drive_max_motivation_bonus,
                   spontaneity_max_messages_per_24h, updated_at
            FROM autonomy_tuning
            WHERE chat_id = ?
            """,
            (str(chat_id),),
        ).fetchone()
        if row is None:
            return AutonomyTuning()
        return AutonomyTuning(
            min_user_idle=timedelta(seconds=int(row["min_user_idle_seconds"])),
            check_interval=timedelta(seconds=int(row["check_interval_seconds"])),
            max_messages_per_24h=int(row["max_messages_per_24h"]),
            idle_drive_max_motivation_bonus=float(row["idle_drive_max_motivation_bonus"]),
            spontaneity_max_messages_per_24h=(
                None
                if row["spontaneity_max_messages_per_24h"] is None
                else int(row["spontaneity_max_messages_per_24h"])
            ),
            updated_at=datetime.fromtimestamp(int(row["updated_at"]), tz=UTC),
        )

    def set_min_user_idle(self, chat_id: int, value: timedelta) -> AutonomyTuning:
        current = replace(self.get(chat_id), min_user_idle=value)
        self._ensure_row(chat_id)
        self._update(chat_id, "min_user_idle_seconds", int(current.min_user_idle.total_seconds()))
        return self.get(chat_id)

    def set_check_interval(self, chat_id: int, value: timedelta) -> AutonomyTuning:
        current = replace(self.get(chat_id), check_interval=value)
        self._ensure_row(chat_id)
        self._update(chat_id, "check_interval_seconds", int(current.check_interval.total_seconds()))
        return self.get(chat_id)

    def set_daily_cap(self, chat_id: int, value: int) -> AutonomyTuning:
        current = replace(self.get(chat_id), max_messages_per_24h=value)
        self._ensure_row(chat_id)
        self._update(chat_id, "max_messages_per_24h", current.max_messages_per_24h)
        return self.get(chat_id)

    def set_idle_drive_bonus(self, chat_id: int, value: float) -> AutonomyTuning:
        current = replace(self.get(chat_id), idle_drive_max_motivation_bonus=value)
        self._ensure_row(chat_id)
        self._update(
            chat_id,
            "idle_drive_max_motivation_bonus",
            current.idle_drive_max_motivation_bonus,
        )
        return self.get(chat_id)

    def set_spontaneity_daily_cap(self, chat_id: int, value: int | None) -> AutonomyTuning:
        current = replace(self.get(chat_id), spontaneity_max_messages_per_24h=value)
        self._ensure_row(chat_id)
        self._update(
            chat_id,
            "spontaneity_max_messages_per_24h",
            current.spontaneity_max_messages_per_24h,
        )
        return self.get(chat_id)

    def reset(self, chat_id: int) -> AutonomyTuning:
        self._validate_chat_id(chat_id)
        with self._db:
            self._db.execute("DELETE FROM autonomy_tuning WHERE chat_id = ?", (str(chat_id),))
        return AutonomyTuning()

    def _ensure_row(self, chat_id: int) -> None:
        self._validate_chat_id(chat_id)
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO autonomy_tuning(
                  chat_id, min_user_idle_seconds, check_interval_seconds,
                  max_messages_per_24h, idle_drive_max_motivation_bonus,
                  spontaneity_max_messages_per_24h, updated_at
                ) VALUES (?, 180, 300, 24, 0.25, NULL, ?)
                """,
                (str(chat_id), int(time.time())),
            )

    def _update(self, chat_id: int, column: str, value: int | float | None) -> None:
        allowed = {
            "min_user_idle_seconds",
            "check_interval_seconds",
            "max_messages_per_24h",
            "idle_drive_max_motivation_bonus",
            "spontaneity_max_messages_per_24h",
        }
        if column not in allowed:
            raise ValueError("unsupported autonomy tuning field")
        with self._db:
            self._db.execute(
                f"UPDATE autonomy_tuning SET {column} = ?, updated_at = ? WHERE chat_id = ?",
                (value, int(time.time()), str(chat_id)),
            )

    @staticmethod
    def _validate_chat_id(chat_id: int) -> None:
        if chat_id <= 0:
            raise ValueError("chat_id must be positive")


class RuntimeAutonomyTuningControl:
    """Bind persistent per-chat tuning to the current autonomy evaluation task."""

    def __init__(self, store: SQLiteAutonomyTuningStore) -> None:
        self._store = store
        self._chat_id: ContextVar[int | None] = ContextVar(
            "amadeus_autonomy_tuning_chat_id",
            default=None,
        )

    def close(self) -> None:
        self._store.close()

    def get(self, chat_id: int) -> AutonomyTuning:
        return self._store.get(chat_id)

    def current(self) -> AutonomyTuning | None:
        chat_id = self._chat_id.get()
        return None if chat_id is None else self._store.get(chat_id)

    @contextmanager
    def use_chat(self, chat_id: int) -> Iterator[AutonomyTuning]:
        tuning = self._store.get(chat_id)
        token = self._chat_id.set(chat_id)
        try:
            yield tuning
        finally:
            self._chat_id.reset(token)

    def set_min_user_idle(self, chat_id: int, value: timedelta) -> AutonomyTuning:
        return self._store.set_min_user_idle(chat_id, value)

    def set_check_interval(self, chat_id: int, value: timedelta) -> AutonomyTuning:
        return self._store.set_check_interval(chat_id, value)

    def set_daily_cap(self, chat_id: int, value: int) -> AutonomyTuning:
        return self._store.set_daily_cap(chat_id, value)

    def set_idle_drive_bonus(self, chat_id: int, value: float) -> AutonomyTuning:
        return self._store.set_idle_drive_bonus(chat_id, value)

    def set_spontaneity_daily_cap(self, chat_id: int, value: int | None) -> AutonomyTuning:
        return self._store.set_spontaneity_daily_cap(chat_id, value)

    def reset(self, chat_id: int) -> AutonomyTuning:
        return self._store.reset(chat_id)


class TunableAutonomyGuard(AutonomyGuard):
    """AutonomyGuard facade whose safe subset of config is resolved per chat at call time."""

    def __init__(
        self,
        base_config: AutonomyGuardConfig,
        tuning: RuntimeAutonomyTuningControl,
    ) -> None:
        super().__init__(base_config)
        self._base_config = base_config
        self._tuning = tuning

    @property
    def config(self) -> AutonomyGuardConfig:
        current = self._tuning.current()
        if current is None:
            return self._base_config
        return replace(
            self._base_config,
            min_user_idle=current.min_user_idle,
            max_messages_per_24h=current.max_messages_per_24h,
            idle_drive_max_motivation_bonus=current.idle_drive_max_motivation_bonus,
        )

    def effective_proactive_cooldown(self, opportunity: AutonomyOpportunity) -> timedelta:
        return self._effective().effective_proactive_cooldown(opportunity)

    def contact_drive_motivation_bonus(self, opportunity: AutonomyOpportunity) -> float:
        return self._effective().contact_drive_motivation_bonus(opportunity)

    def effective_model_motivation(
        self,
        motivation: float,
        opportunity: AutonomyOpportunity,
    ) -> float:
        return self._effective().effective_model_motivation(motivation, opportunity)

    def evaluate(self, opportunity: AutonomyOpportunity) -> AutonomyGuardResult:
        return self._effective().evaluate(opportunity)

    def _effective(self) -> AutonomyGuard:
        return AutonomyGuard(self.config)


class TunableAutonomyOpportunityScheduler(AutonomyOpportunityScheduler):
    """Opportunity scheduler whose interval is resolved from the current chat tuning."""

    def __init__(
        self,
        base_config: AutonomyScheduleConfig,
        tuning: RuntimeAutonomyTuningControl,
    ) -> None:
        super().__init__(base_config)
        self._base_config = base_config
        self._tuning = tuning

    @property
    def config(self) -> AutonomyScheduleConfig:
        current = self._tuning.current()
        if current is None:
            return self._base_config
        return replace(self._base_config, check_interval=current.check_interval)

    def is_due(self, *, now: datetime, last_opportunity_at: datetime | None) -> bool:
        return self._effective().is_due(now=now, last_opportunity_at=last_opportunity_at)

    def next_due(self, *, last_opportunity_at: datetime) -> datetime:
        return self._effective().next_due(last_opportunity_at=last_opportunity_at)

    def _effective(self) -> AutonomyOpportunityScheduler:
        return AutonomyOpportunityScheduler(self.config)
