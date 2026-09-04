from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal


class LegacyMemoryKind(StrEnum):
    PROFILE = "profile"
    PREFERENCE = "preference"
    COMMITMENT = "commitment"
    EVENT = "event"
    EXPLICIT = "explicit"


MemorySource = Literal["manual", "automatic"]


@dataclass(frozen=True, slots=True)
class LegacyMemoryItem:
    memory_id: int
    kind: LegacyMemoryKind
    content: str
    importance: float
    source_type: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class AutomaticMemoryCandidate:
    kind: LegacyMemoryKind
    content: str
    importance: float


_SENSITIVE_PATTERN = re.compile(
    r"(?:password|passwd|密码|口令|验证码|verification\s*code|api[ _-]?key|"
    r"access[ _-]?token|refresh[ _-]?token|private[ _-]?key|私钥|助记词|seed\s*phrase|"
    r"身份证|护照号|银行卡|信用卡|cvv|病历|诊断)",
    re.IGNORECASE,
)
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,12}|[a-z0-9_]{2,}", re.IGNORECASE)


def _normalize(content: str) -> str:
    return " ".join(content.strip().split()).casefold()


def _now_seconds() -> int:
    return int(time.time())


def is_sensitive_memory(content: str) -> bool:
    return bool(_SENSITIVE_PATTERN.search(content))


def derive_automatic_memories(user_text: str) -> tuple[AutomaticMemoryCandidate, ...]:
    text = " ".join(user_text.strip().split())
    if len(text) < 3 or len(text) > 500 or is_sensitive_memory(text):
        return ()

    sentences = [
        sentence.strip()
        for sentence in re.split(r"[。！？!?；;\n]+", text)
        if 3 <= len(sentence.strip()) <= 240
    ]
    results: list[AutomaticMemoryCandidate] = []
    for sentence in sentences:
        if re.match(
            r"^(?:我叫|我的名字是|可以叫我|请叫我|我的昵称是|我(?:目前)?住在|我的时区是|"
            r"my name is|call me|i live in|my timezone is)",
            sentence,
            re.IGNORECASE,
        ):
            results.append(
                AutomaticMemoryCandidate(LegacyMemoryKind.PROFILE, sentence, 0.9)
            )
        elif re.match(
            r"^(?:我喜欢|我不喜欢|我偏好|我的偏好是|我更喜欢|以后(?:请|不要)|请尽量|"
            r"我希望你|i (?:like|dislike|prefer)|please (?:always|avoid)|i want you to)",
            sentence,
            re.IGNORECASE,
        ):
            results.append(
                AutomaticMemoryCandidate(LegacyMemoryKind.PREFERENCE, sentence, 0.78)
            )
        elif re.match(
            r"^(?:我计划|我打算|我准备在|我答应|记得提醒我|i plan to|i intend to|i promised)",
            sentence,
            re.IGNORECASE,
        ):
            results.append(
                AutomaticMemoryCandidate(LegacyMemoryKind.COMMITMENT, sentence, 0.68)
            )
        if len(results) >= 3:
            break
    return tuple(results)


class LegacyMemoryStore:
    """SQLite compatibility layer matching the production Amadeus v1 memory schema."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_settings (
              chat_id TEXT PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 1,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_items (
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
            CREATE INDEX IF NOT EXISTS memory_items_chat_status
              ON memory_items(chat_id, status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS memory_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              memory_id INTEGER,
              action TEXT NOT NULL,
              detail TEXT,
              created_at INTEGER NOT NULL
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def is_enabled(self, chat_id: int) -> bool:
        row = self._db.execute(
            "SELECT enabled FROM memory_settings WHERE chat_id = ?", (str(chat_id),)
        ).fetchone()
        return row is None or int(row["enabled"]) != 0

    def set_enabled(self, chat_id: int, enabled: bool) -> None:
        now = _now_seconds()
        self._db.execute(
            """
            INSERT INTO memory_settings(chat_id, enabled, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
              enabled = excluded.enabled,
              updated_at = excluded.updated_at
            """,
            (str(chat_id), 1 if enabled else 0, now),
        )
        self._audit(chat_id, None, "enable" if enabled else "disable", None)
        self._db.commit()

    def remember(
        self,
        chat_id: int,
        content: str,
        *,
        kind: LegacyMemoryKind = LegacyMemoryKind.EXPLICIT,
        importance: float = 0.8,
        source_type: MemorySource = "manual",
    ) -> LegacyMemoryItem:
        cleaned = " ".join(content.strip().split())
        if len(cleaned) < 2 or len(cleaned) > 500:
            raise ValueError("记忆内容需要在 2 到 500 个字符之间。")
        if is_sensitive_memory(cleaned):
            raise ValueError("这段内容可能含凭据、身份或健康敏感信息，未保存。")

        normalized = _normalize(cleaned)
        now = _now_seconds()
        bounded_importance = max(0.0, min(1.0, importance))
        self._db.execute(
            """
            INSERT INTO memory_items(
              chat_id, kind, content, normalized_content, importance, source_type,
              created_at, updated_at, last_accessed_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(chat_id, normalized_content) DO UPDATE SET
              kind = excluded.kind,
              content = excluded.content,
              importance = MAX(memory_items.importance, excluded.importance),
              source_type = excluded.source_type,
              updated_at = excluded.updated_at,
              status = 'active'
            """,
            (
                str(chat_id),
                kind.value,
                cleaned,
                normalized,
                bounded_importance,
                source_type,
                now,
                now,
                now,
            ),
        )
        row = self._db.execute(
            """
            SELECT id, kind, content, importance, source_type, created_at, updated_at
            FROM memory_items WHERE chat_id = ? AND normalized_content = ?
            """,
            (str(chat_id), normalized),
        ).fetchone()
        if row is None:
            raise RuntimeError("memory upsert completed without a readable row")
        item = self._as_item(row)
        self._audit(
            chat_id,
            item.memory_id,
            "remember" if source_type == "manual" else "auto_remember",
            kind.value,
        )
        self._db.commit()
        return item

    def list(
        self,
        chat_id: int,
        limit: int = 20,
        kinds: tuple[LegacyMemoryKind, ...] | None = None,
    ) -> tuple[LegacyMemoryItem, ...]:
        safe_limit = max(1, min(50, limit))
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            rows = self._db.execute(
                f"""
                SELECT id, kind, content, importance, source_type, created_at, updated_at
                FROM memory_items
                WHERE chat_id = ? AND status = 'active' AND kind IN ({placeholders})
                ORDER BY importance DESC, updated_at DESC LIMIT ?
                """,
                (str(chat_id), *(kind.value for kind in kinds), safe_limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                """
                SELECT id, kind, content, importance, source_type, created_at, updated_at
                FROM memory_items
                WHERE chat_id = ? AND status = 'active'
                ORDER BY importance DESC, updated_at DESC LIMIT ?
                """,
                (str(chat_id), safe_limit),
            ).fetchall()
        return tuple(self._as_item(row) for row in rows)

    def forget(self, chat_id: int, selector: str) -> tuple[LegacyMemoryItem, ...]:
        cleaned = selector.strip()
        if not cleaned:
            return ()
        if cleaned.isdigit():
            rows = self._db.execute(
                """
                SELECT id, kind, content, importance, source_type, created_at, updated_at
                FROM memory_items WHERE chat_id = ? AND id = ? AND status = 'active'
                """,
                (str(chat_id), int(cleaned)),
            ).fetchall()
        else:
            rows = self._db.execute(
                """
                SELECT id, kind, content, importance, source_type, created_at, updated_at
                FROM memory_items
                WHERE chat_id = ? AND status = 'active' AND normalized_content LIKE ?
                ORDER BY updated_at DESC LIMIT 10
                """,
                (str(chat_id), f"%{_normalize(cleaned)}%"),
            ).fetchall()
        items = tuple(self._as_item(row) for row in rows)
        now = _now_seconds()
        for item in items:
            self._db.execute(
                """
                UPDATE memory_items SET status = 'deleted', updated_at = ?
                WHERE chat_id = ? AND id = ?
                """,
                (now, str(chat_id), item.memory_id),
            )
            self._audit(chat_id, item.memory_id, "forget", None)
        self._db.commit()
        return items

    def retrieve(self, chat_id: int, query: str, limit: int = 8) -> tuple[LegacyMemoryItem, ...]:
        if not self.is_enabled(chat_id):
            return ()
        candidates = self.list(chat_id, 50)
        if not candidates:
            return ()
        tokens = list(dict.fromkeys(_TOKEN_PATTERN.findall(_normalize(query))))[:12]
        now = _now_seconds()
        scored: list[tuple[float, LegacyMemoryItem]] = []
        for item in candidates:
            content = _normalize(item.content)
            matches = sum(1 for token in tokens if token in content)
            relevance = matches / len(tokens) if tokens else 0.0
            age_days = max(0.0, (now - item.updated_at) / 86400)
            freshness = 1 / (1 + age_days / 30)
            score = relevance * 0.62 + item.importance * 0.28 + freshness * 0.1
            scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
        selected = [
            item
            for index, (score, item) in enumerate(scored)
            if score >= 0.22 or index < 3
        ][: max(1, min(12, limit))]
        for item in selected:
            self._db.execute(
                "UPDATE memory_items SET last_accessed_at = ? WHERE chat_id = ? AND id = ?",
                (now, str(chat_id), item.memory_id),
            )
        self._db.commit()
        return tuple(selected)

    def _audit(
        self,
        chat_id: int,
        memory_id: int | None,
        action: str,
        detail: str | None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO memory_audit(chat_id, memory_id, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(chat_id), memory_id, action, detail, _now_seconds()),
        )

    @staticmethod
    def _as_item(row: sqlite3.Row) -> LegacyMemoryItem:
        return LegacyMemoryItem(
            memory_id=int(row["id"]),
            kind=LegacyMemoryKind(str(row["kind"])),
            content=str(row["content"]),
            importance=float(row["importance"]),
            source_type=str(row["source_type"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )
