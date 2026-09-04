from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path

_SLEEP_START_PHRASES = ("晚安", "哦呀斯密", "おやすみ", "お休み", "oyasumi")
_WAKE_PHRASES = ("早上好", "早安", "おはよう", "ohayou")
_MAX_GREETING_CHARS = 24


@dataclass(frozen=True, slots=True)
class AutonomyPreferences:
    """Persistent per-chat controls for autonomy, spontaneity and semantic sleep sessions."""

    enabled: bool = False
    spontaneity_enabled: bool = False
    do_not_disturb: bool = False
    quiet_enabled: bool = True
    quiet_start_minute: int = 0
    quiet_end_minute: int = 8 * 60
    sleep_mode: bool = False
    sleep_started_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for name, minute_value in (
            ("quiet_start_minute", self.quiet_start_minute),
            ("quiet_end_minute", self.quiet_end_minute),
        ):
            if not 0 <= minute_value < 24 * 60:
                raise ValueError(f"{name} must be within a day")
        if self.quiet_start_minute == self.quiet_end_minute:
            raise ValueError("quiet hours start and end must differ")
        for name, timestamp_value in (
            ("sleep_started_at", self.sleep_started_at),
            ("updated_at", self.updated_at),
        ):
            if timestamp_value is not None and (
                timestamp_value.tzinfo is None or timestamp_value.utcoffset() is None
            ):
                raise ValueError(f"autonomy preference {name} must be timezone-aware")
        if self.sleep_mode and self.sleep_started_at is None:
            raise ValueError("sleep_mode requires sleep_started_at")

    def quiet_active(self, at: datetime, *, timezone: tzinfo) -> bool:
        """Legacy fixed quiet-window helper retained for DB/command compatibility."""

        if not self.quiet_enabled:
            return False
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("autonomy quiet-hours timestamp must be timezone-aware")
        local = at.astimezone(timezone)
        minute = local.hour * 60 + local.minute
        start = self.quiet_start_minute
        end = self.quiet_end_minute
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end

    @property
    def quiet_window(self) -> str:
        return f"{self._format_minute(self.quiet_start_minute)}-{self._format_minute(self.quiet_end_minute)}"

    @staticmethod
    def _format_minute(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"


class SQLiteRuntimePreferenceStore:
    """Persistent per-chat runtime preferences that must survive process restarts."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_preferences (
              chat_id TEXT PRIMARY KEY,
              memory_enabled INTEGER NOT NULL CHECK(memory_enabled IN (0, 1)),
              updated_at INTEGER NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS autonomy_preferences (
              chat_id TEXT PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
              spontaneity_enabled INTEGER NOT NULL DEFAULT 0 CHECK(spontaneity_enabled IN (0, 1)),
              do_not_disturb INTEGER NOT NULL DEFAULT 0 CHECK(do_not_disturb IN (0, 1)),
              quiet_enabled INTEGER NOT NULL DEFAULT 1 CHECK(quiet_enabled IN (0, 1)),
              quiet_start_minute INTEGER NOT NULL DEFAULT 0
                CHECK(quiet_start_minute >= 0 AND quiet_start_minute < 1440),
              quiet_end_minute INTEGER NOT NULL DEFAULT 480
                CHECK(quiet_end_minute >= 0 AND quiet_end_minute < 1440),
              sleep_mode INTEGER NOT NULL DEFAULT 0 CHECK(sleep_mode IN (0, 1)),
              sleep_started_at INTEGER,
              updated_at INTEGER NOT NULL
            )
            """
        )
        columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(autonomy_preferences)").fetchall()
        }
        if "spontaneity_enabled" not in columns:
            self._db.execute(
                "ALTER TABLE autonomy_preferences "
                "ADD COLUMN spontaneity_enabled INTEGER NOT NULL DEFAULT 0 "
                "CHECK(spontaneity_enabled IN (0, 1))"
            )
        if "sleep_mode" not in columns:
            self._db.execute(
                "ALTER TABLE autonomy_preferences "
                "ADD COLUMN sleep_mode INTEGER NOT NULL DEFAULT 0 CHECK(sleep_mode IN (0, 1))"
            )
        if "sleep_started_at" not in columns:
            self._db.execute("ALTER TABLE autonomy_preferences ADD COLUMN sleep_started_at INTEGER")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def memory_enabled(self, chat_id: int) -> bool:
        row = self._db.execute(
            "SELECT memory_enabled FROM chat_preferences WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        return True if row is None else bool(int(row["memory_enabled"]))

    def set_memory_enabled(self, chat_id: int, enabled: bool) -> None:
        self._validate_chat_id(chat_id)
        with self._db:
            self._db.execute(
                """
                INSERT INTO chat_preferences(chat_id, memory_enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  memory_enabled = excluded.memory_enabled,
                  updated_at = excluded.updated_at
                """,
                (str(chat_id), int(enabled), int(time.time())),
            )

    def autonomy_preferences(self, chat_id: int) -> AutonomyPreferences:
        self._validate_chat_id(chat_id)
        row = self._db.execute(
            """
            SELECT enabled, spontaneity_enabled, do_not_disturb, quiet_enabled,
                   quiet_start_minute, quiet_end_minute,
                   sleep_mode, sleep_started_at, updated_at
            FROM autonomy_preferences WHERE chat_id = ?
            """,
            (str(chat_id),),
        ).fetchone()
        if row is None:
            return AutonomyPreferences()
        return AutonomyPreferences(
            enabled=bool(int(row["enabled"])),
            spontaneity_enabled=bool(int(row["spontaneity_enabled"])),
            do_not_disturb=bool(int(row["do_not_disturb"])),
            quiet_enabled=bool(int(row["quiet_enabled"])),
            quiet_start_minute=int(row["quiet_start_minute"]),
            quiet_end_minute=int(row["quiet_end_minute"]),
            sleep_mode=bool(int(row["sleep_mode"])),
            sleep_started_at=(
                datetime.fromtimestamp(int(row["sleep_started_at"]), tz=UTC)
                if row["sleep_started_at"] is not None
                else None
            ),
            updated_at=datetime.fromtimestamp(int(row["updated_at"]), tz=UTC),
        )

    def set_autonomy_enabled(self, chat_id: int, enabled: bool) -> None:
        self._ensure_autonomy_row(chat_id)
        self._update_autonomy(chat_id, "enabled", int(enabled))

    def set_spontaneity_enabled(self, chat_id: int, enabled: bool) -> None:
        self._ensure_autonomy_row(chat_id)
        self._update_autonomy(chat_id, "spontaneity_enabled", int(enabled))

    def set_autonomy_dnd(self, chat_id: int, enabled: bool) -> None:
        self._ensure_autonomy_row(chat_id)
        self._update_autonomy(chat_id, "do_not_disturb", int(enabled))

    def set_autonomy_quiet_enabled(self, chat_id: int, enabled: bool) -> None:
        self._ensure_autonomy_row(chat_id)
        self._update_autonomy(chat_id, "quiet_enabled", int(enabled))

    def set_autonomy_quiet_hours(self, chat_id: int, *, start_minute: int, end_minute: int) -> None:
        value = AutonomyPreferences(
            quiet_start_minute=start_minute,
            quiet_end_minute=end_minute,
        )
        self._ensure_autonomy_row(chat_id)
        with self._db:
            self._db.execute(
                """
                UPDATE autonomy_preferences
                SET quiet_enabled = 1, quiet_start_minute = ?, quiet_end_minute = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (
                    value.quiet_start_minute,
                    value.quiet_end_minute,
                    int(time.time()),
                    str(chat_id),
                ),
            )

    def observe_user_text(self, chat_id: int, text: str, *, at: datetime) -> str | None:
        """Update semantic sleep state from a short greeting-like user utterance."""

        self._validate_chat_id(chat_id)
        self._validate_aware(at)
        normalized = self._normalize_greeting(text)
        if not normalized:
            return None
        current = self.autonomy_preferences(chat_id)
        timestamp = int(at.timestamp())
        if self._matches_greeting(normalized, _SLEEP_START_PHRASES):
            if current.sleep_mode:
                return None
            self._ensure_autonomy_row(chat_id)
            with self._db:
                self._db.execute(
                    """
                    UPDATE autonomy_preferences
                    SET sleep_mode = 1, sleep_started_at = ?
                    WHERE chat_id = ?
                    """,
                    (timestamp, str(chat_id)),
                )
            return "sleep_started"
        if self._matches_greeting(normalized, _WAKE_PHRASES):
            if not current.sleep_mode:
                return None
            self._ensure_autonomy_row(chat_id)
            with self._db:
                self._db.execute(
                    """
                    UPDATE autonomy_preferences
                    SET sleep_mode = 0, sleep_started_at = NULL
                    WHERE chat_id = ?
                    """,
                    (str(chat_id),),
                )
            return "sleep_ended"
        return None

    def _ensure_autonomy_row(self, chat_id: int) -> None:
        self._validate_chat_id(chat_id)
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO autonomy_preferences(
                  chat_id, enabled, spontaneity_enabled, do_not_disturb, quiet_enabled,
                  quiet_start_minute, quiet_end_minute,
                  sleep_mode, sleep_started_at, updated_at
                ) VALUES (?, 0, 0, 0, 1, 0, 480, 0, NULL, ?)
                """,
                (str(chat_id), int(time.time())),
            )

    def _update_autonomy(self, chat_id: int, column: str, value: int) -> None:
        allowed = {"enabled", "spontaneity_enabled", "do_not_disturb", "quiet_enabled"}
        if column not in allowed:
            raise ValueError("unsupported autonomy preference field")
        with self._db:
            self._db.execute(
                f"UPDATE autonomy_preferences SET {column} = ?, updated_at = ? WHERE chat_id = ?",
                (value, int(time.time()), str(chat_id)),
            )

    @staticmethod
    def _normalize_greeting(text: str) -> str:
        value = "".join(character for character in text.strip().casefold() if character.isalnum())
        if not value or len(value) > _MAX_GREETING_CHARS:
            return ""
        return value

    @classmethod
    def _matches_greeting(cls, value: str, phrases: tuple[str, ...]) -> bool:
        for phrase in phrases:
            normalized_phrase = cls._normalize_greeting(phrase)
            if value == normalized_phrase:
                return True
            if value.startswith(normalized_phrase) or value.endswith(normalized_phrase):
                return True
        return False

    @staticmethod
    def _validate_chat_id(chat_id: int) -> None:
        if chat_id <= 0:
            raise ValueError("chat_id must be positive")

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("autonomy preference timestamp must be timezone-aware")
