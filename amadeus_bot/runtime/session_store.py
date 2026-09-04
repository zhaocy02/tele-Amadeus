from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.llm import LLMMessage, MessageRole


@dataclass(frozen=True, slots=True)
class StoredConversationMessage:
    message_id: int
    role: MessageRole
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredConversationExchange:
    turn_id: str
    user_message_id: int
    assistant_message_id: int


@dataclass(frozen=True, slots=True)
class StoredAutonomyConversationMessage:
    assistant_message_id: int
    generation: int
    action: str
    signal_id: str
    telegram_message_id: int


@dataclass(frozen=True, slots=True)
class StoredSpontaneityConversationMessage:
    assistant_message_id: int
    generation: int
    source_turn_id: str
    telegram_message_id: int


class ConversationSessionStore:
    """Persistent per-chat conversation history with explicit generation rotation."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation_sessions (
              chat_id TEXT PRIMARY KEY,
              generation INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
              content TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS conversation_messages_active
              ON conversation_messages(chat_id, generation, id DESC);
            CREATE TABLE IF NOT EXISTS v2_turn_metadata (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              turn_id TEXT NOT NULL UNIQUE,
              chat_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              user_message_id INTEGER NOT NULL UNIQUE,
              assistant_message_id INTEGER NOT NULL UNIQUE,
              policy_act TEXT NOT NULL,
              created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS v2_turn_metadata_recent
              ON v2_turn_metadata(chat_id, generation, id DESC);
            CREATE TABLE IF NOT EXISTS v2_autonomy_message_metadata (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              assistant_message_id INTEGER NOT NULL UNIQUE,
              action TEXT NOT NULL,
              signal_id TEXT NOT NULL,
              telegram_message_id INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              UNIQUE(chat_id, telegram_message_id)
            );
            CREATE INDEX IF NOT EXISTS v2_autonomy_message_recent
              ON v2_autonomy_message_metadata(chat_id, generation, id DESC);
            CREATE TABLE IF NOT EXISTS v2_spontaneity_message_metadata (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              assistant_message_id INTEGER NOT NULL UNIQUE,
              source_turn_id TEXT NOT NULL UNIQUE,
              telegram_message_id INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              UNIQUE(chat_id, telegram_message_id)
            );
            CREATE INDEX IF NOT EXISTS v2_spontaneity_message_recent
              ON v2_spontaneity_message_metadata(chat_id, generation, id DESC);
            CREATE TABLE IF NOT EXISTS v2_runtime_markers (
              chat_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              last_retrospective_turn_count INTEGER NOT NULL DEFAULT 0,
              last_retrospective_at INTEGER,
              PRIMARY KEY(chat_id, generation)
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def current_generation(self, chat_id: int) -> int:
        row = self._db.execute(
            "SELECT generation FROM conversation_sessions WHERE chat_id = ?",
            (str(chat_id),),
        ).fetchone()
        if row is not None:
            return int(row["generation"])
        now = int(time.time())
        self._db.execute(
            "INSERT INTO conversation_sessions(chat_id, generation, updated_at) VALUES (?, 1, ?)",
            (str(chat_id), now),
        )
        self._db.commit()
        return 1

    def rotate(self, chat_id: int) -> int:
        generation = self.current_generation(chat_id) + 1
        self._db.execute(
            """
            UPDATE conversation_sessions SET generation = ?, updated_at = ? WHERE chat_id = ?
            """,
            (generation, int(time.time()), str(chat_id)),
        )
        self._db.commit()
        return generation

    def append_exchange(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        generation = self.current_generation(chat_id)
        now = int(time.time())
        with self._db:
            self._append_messages(
                chat_id=chat_id,
                generation=generation,
                user_text=user_text,
                assistant_text=assistant_text,
                created_at=now,
            )
            self._touch_session(chat_id, now)

    def append_v2_exchange(
        self,
        chat_id: int,
        user_text: str,
        assistant_text: str,
        *,
        turn_id: str,
        policy_act: str,
    ) -> StoredConversationExchange:
        """Atomically persist transcript rows and v2 policy metadata for one delivered turn."""

        normalized_turn_id = turn_id.strip()
        normalized_act = policy_act.strip()
        if not normalized_turn_id:
            raise ValueError("turn_id must not be empty")
        if not normalized_act:
            raise ValueError("policy_act must not be empty")
        generation = self.current_generation(chat_id)
        now = int(time.time())
        with self._db:
            user_id, assistant_id = self._append_messages(
                chat_id=chat_id,
                generation=generation,
                user_text=user_text,
                assistant_text=assistant_text,
                created_at=now,
            )
            self._db.execute(
                """
                INSERT INTO v2_turn_metadata(
                  turn_id, chat_id, generation, user_message_id,
                  assistant_message_id, policy_act, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_turn_id,
                    str(chat_id),
                    generation,
                    user_id,
                    assistant_id,
                    normalized_act,
                    now,
                ),
            )
            self._touch_session(chat_id, now)
        return StoredConversationExchange(
            turn_id=normalized_turn_id,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
        )

    def append_v2_autonomy_message(
        self,
        chat_id: int,
        assistant_text: str,
        *,
        generation: int,
        action: str,
        signal_id: str,
        telegram_message_id: int,
        at: datetime,
    ) -> StoredAutonomyConversationMessage:
        """Persist one externally delivered proactive assistant message
        without inventing a user row.
        """

        text = assistant_text.strip()
        normalized_action = action.strip()
        normalized_signal = signal_id.strip()
        if not text:
            raise ValueError("autonomy assistant text must not be empty")
        if generation < 1:
            raise ValueError("autonomy conversation generation must be positive")
        if not normalized_action or not normalized_signal:
            raise ValueError("autonomy action/signal must not be empty")
        if telegram_message_id <= 0:
            raise ValueError("Telegram message ID must be positive")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("autonomy transcript timestamp must be timezone-aware")

        existing = self._db.execute(
            """
            SELECT assistant_message_id, generation, action, signal_id, telegram_message_id
            FROM v2_autonomy_message_metadata
            WHERE chat_id = ? AND telegram_message_id = ?
            """,
            (str(chat_id), telegram_message_id),
        ).fetchone()
        if existing is not None:
            return StoredAutonomyConversationMessage(
                assistant_message_id=int(existing["assistant_message_id"]),
                generation=int(existing["generation"]),
                action=str(existing["action"]),
                signal_id=str(existing["signal_id"]),
                telegram_message_id=int(existing["telegram_message_id"]),
            )

        timestamp = int(at.timestamp())
        current_generation = self.current_generation(chat_id)
        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO conversation_messages(chat_id, generation, role, content, created_at)
                VALUES (?, ?, 'assistant', ?, ?)
                """,
                (str(chat_id), generation, text, timestamp),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return autonomy assistant message ID")
            assistant_message_id = int(cursor.lastrowid)
            self._db.execute(
                """
                INSERT INTO v2_autonomy_message_metadata(
                  chat_id, generation, assistant_message_id, action,
                  signal_id, telegram_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    generation,
                    assistant_message_id,
                    normalized_action,
                    normalized_signal,
                    telegram_message_id,
                    timestamp,
                ),
            )
            if current_generation == generation:
                self._touch_session(chat_id, timestamp)
        return StoredAutonomyConversationMessage(
            assistant_message_id=assistant_message_id,
            generation=generation,
            action=normalized_action,
            signal_id=normalized_signal,
            telegram_message_id=telegram_message_id,
        )

    def append_v2_spontaneity_message(
        self,
        chat_id: int,
        assistant_text: str,
        *,
        generation: int,
        source_turn_id: str,
        telegram_message_id: int,
        at: datetime,
    ) -> StoredSpontaneityConversationMessage:
        """Persist one delayed continuation as a standalone assistant transcript row."""

        text = assistant_text.strip()
        source = source_turn_id.strip()
        if not text:
            raise ValueError("spontaneity assistant text must not be empty")
        if generation < 1:
            raise ValueError("spontaneity conversation generation must be positive")
        if not source:
            raise ValueError("spontaneity source_turn_id must not be empty")
        if telegram_message_id <= 0:
            raise ValueError("Telegram message ID must be positive")
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("spontaneity transcript timestamp must be timezone-aware")

        existing = self._db.execute(
            """
            SELECT assistant_message_id, generation, source_turn_id, telegram_message_id
            FROM v2_spontaneity_message_metadata WHERE source_turn_id = ?
            """,
            (source,),
        ).fetchone()
        if existing is not None:
            return StoredSpontaneityConversationMessage(
                assistant_message_id=int(existing["assistant_message_id"]),
                generation=int(existing["generation"]),
                source_turn_id=str(existing["source_turn_id"]),
                telegram_message_id=int(existing["telegram_message_id"]),
            )

        timestamp = int(at.timestamp())
        current_generation = self.current_generation(chat_id)
        with self._db:
            cursor = self._db.execute(
                """
                INSERT INTO conversation_messages(chat_id, generation, role, content, created_at)
                VALUES (?, ?, 'assistant', ?, ?)
                """,
                (str(chat_id), generation, text, timestamp),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return spontaneity assistant message ID")
            assistant_message_id = int(cursor.lastrowid)
            self._db.execute(
                """
                INSERT INTO v2_spontaneity_message_metadata(
                  chat_id, generation, assistant_message_id, source_turn_id,
                  telegram_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chat_id),
                    generation,
                    assistant_message_id,
                    source,
                    telegram_message_id,
                    timestamp,
                ),
            )
            if current_generation == generation:
                self._touch_session(chat_id, timestamp)
        return StoredSpontaneityConversationMessage(
            assistant_message_id=assistant_message_id,
            generation=generation,
            source_turn_id=source,
            telegram_message_id=telegram_message_id,
        )

    def history(self, chat_id: int, limit_messages: int = 20) -> tuple[LLMMessage, ...]:
        return tuple(
            LLMMessage(role=item.role, content=item.content)
            for item in self.history_records(chat_id, limit_messages)
        )

    def history_records(
        self,
        chat_id: int,
        limit_messages: int = 20,
    ) -> tuple[StoredConversationMessage, ...]:
        generation = self.current_generation(chat_id)
        safe_limit = max(0, min(100, limit_messages))
        if safe_limit == 0:
            return ()
        rows = self._db.execute(
            """
            SELECT id, role, content, created_at FROM conversation_messages
            WHERE chat_id = ? AND generation = ?
            ORDER BY id DESC LIMIT ?
            """,
            (str(chat_id), generation, safe_limit),
        ).fetchall()
        return tuple(
            StoredConversationMessage(
                message_id=int(row["id"]),
                role=MessageRole(str(row["role"])),
                content=str(row["content"]),
                created_at=datetime.fromtimestamp(int(row["created_at"]), tz=UTC),
            )
            for row in reversed(rows)
        )

    def recent_turns(self, chat_id: int, turns: int = 5) -> tuple[LLMMessage, ...]:
        safe_turns = max(1, min(20, turns))
        return self.history(chat_id, safe_turns * 2)

    def last_user_message_id(self, chat_id: int) -> int | None:
        """Return the latest current-generation user row ID as a precise activity marker."""

        generation = self.current_generation(chat_id)
        row = self._db.execute(
            """
            SELECT id FROM conversation_messages
            WHERE chat_id = ? AND generation = ? AND role = 'user'
            ORDER BY id DESC LIMIT 1
            """,
            (str(chat_id), generation),
        ).fetchone()
        return None if row is None else int(row["id"])

    def last_user_message_at(self, chat_id: int) -> datetime | None:
        generation = self.current_generation(chat_id)
        row = self._db.execute(
            """
            SELECT created_at FROM conversation_messages
            WHERE chat_id = ? AND generation = ? AND role = 'user'
            ORDER BY id DESC LIMIT 1
            """,
            (str(chat_id), generation),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromtimestamp(int(row["created_at"]), tz=UTC)

    def recent_policy_acts(self, chat_id: int, limit: int = 6) -> tuple[str, ...]:
        generation = self.current_generation(chat_id)
        safe_limit = max(0, min(50, limit))
        if safe_limit == 0:
            return ()
        rows = self._db.execute(
            """
            SELECT policy_act FROM v2_turn_metadata
            WHERE chat_id = ? AND generation = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (str(chat_id), generation, safe_limit),
        ).fetchall()
        return tuple(str(row["policy_act"]) for row in reversed(rows))

    def v2_turn_count(self, chat_id: int) -> int:
        generation = self.current_generation(chat_id)
        row = self._db.execute(
            """
            SELECT COUNT(*) AS count FROM v2_turn_metadata
            WHERE chat_id = ? AND generation = ?
            """,
            (str(chat_id), generation),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def retrospective_turns_since_last(self, chat_id: int) -> int:
        generation = self.current_generation(chat_id)
        row = self._db.execute(
            """
            SELECT last_retrospective_turn_count FROM v2_runtime_markers
            WHERE chat_id = ? AND generation = ?
            """,
            (str(chat_id), generation),
        ).fetchone()
        last_count = int(row["last_retrospective_turn_count"]) if row is not None else 0
        return max(0, self.v2_turn_count(chat_id) - last_count)

    def mark_retrospective_run(self, chat_id: int, *, at: datetime) -> None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("retrospective marker timestamp must be timezone-aware")
        generation = self.current_generation(chat_id)
        turn_count = self.v2_turn_count(chat_id)
        timestamp = int(at.timestamp())
        with self._db:
            self._db.execute(
                """
                INSERT INTO v2_runtime_markers(
                  chat_id, generation, last_retrospective_turn_count, last_retrospective_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, generation) DO UPDATE SET
                  last_retrospective_turn_count = excluded.last_retrospective_turn_count,
                  last_retrospective_at = excluded.last_retrospective_at
                """,
                (str(chat_id), generation, turn_count, timestamp),
            )

    def message_count(self, chat_id: int) -> int:
        generation = self.current_generation(chat_id)
        row = self._db.execute(
            """
            SELECT COUNT(*) AS count FROM conversation_messages
            WHERE chat_id = ? AND generation = ?
            """,
            (str(chat_id), generation),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _append_messages(
        self,
        *,
        chat_id: int,
        generation: int,
        user_text: str,
        assistant_text: str,
        created_at: int,
    ) -> tuple[int, int]:
        user_cursor = self._db.execute(
            """
            INSERT INTO conversation_messages(chat_id, generation, role, content, created_at)
            VALUES (?, ?, 'user', ?, ?)
            """,
            (str(chat_id), generation, user_text, created_at),
        )
        assistant_cursor = self._db.execute(
            """
            INSERT INTO conversation_messages(chat_id, generation, role, content, created_at)
            VALUES (?, ?, 'assistant', ?, ?)
            """,
            (str(chat_id), generation, assistant_text, created_at),
        )
        if user_cursor.lastrowid is None or assistant_cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return conversation message IDs")
        return int(user_cursor.lastrowid), int(assistant_cursor.lastrowid)

    def _touch_session(self, chat_id: int, updated_at: int) -> None:
        self._db.execute(
            "UPDATE conversation_sessions SET updated_at = ? WHERE chat_id = ?",
            (updated_at, str(chat_id)),
        )
