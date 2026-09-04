from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .state import CharacterState, CharacterStateSnapshot


class SQLiteCharacterStateStore:
    """Persist one current Character State snapshot plus immutable version history."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self._db.close()

    async def load(self) -> CharacterStateSnapshot | None:
        row = self._db.execute(
            """
            SELECT version, updated_at, values_json
            FROM character_state_current
            WHERE singleton = 1
            """
        ).fetchone()
        return self._row_to_snapshot(row) if row is not None else None

    async def save(self, state: CharacterStateSnapshot) -> None:
        current = self._db.execute(
            "SELECT version FROM character_state_current WHERE singleton = 1"
        ).fetchone()
        expected = 1 if current is None else int(current["version"]) + 1
        if state.version != expected:
            raise ValueError(
                f"character state version must advance monotonically: expected {expected}, "
                f"got {state.version}"
            )
        payload = json.dumps(dict(state.values), ensure_ascii=False, sort_keys=True)
        timestamp = state.updated_at.isoformat()
        with self._db:
            self._db.execute(
                """
                INSERT INTO character_state_history(version, updated_at, values_json)
                VALUES (?, ?, ?)
                """,
                (state.version, timestamp, payload),
            )
            self._db.execute(
                """
                INSERT INTO character_state_current(singleton, version, updated_at, values_json)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at,
                    values_json = excluded.values_json
                """,
                (state.version, timestamp, payload),
            )

    async def load_state(self) -> CharacterState | None:
        snapshot = await self.load()
        return CharacterState.from_snapshot(snapshot) if snapshot is not None else None

    async def save_state(self, state: CharacterState) -> None:
        await self.save(state.to_snapshot())

    async def history(self, *, limit: int = 20) -> tuple[CharacterStateSnapshot, ...]:
        safe_limit = max(1, min(200, limit))
        rows = self._db.execute(
            """
            SELECT version, updated_at, values_json
            FROM character_state_history
            ORDER BY version DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return tuple(self._row_to_snapshot(row) for row in rows)

    def _create_schema(self) -> None:
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS character_state_current (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    values_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS character_state_history (
                    version INTEGER PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    values_json TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> CharacterStateSnapshot:
        values = json.loads(str(row["values_json"]))
        if not isinstance(values, dict):
            raise ValueError("character state values_json must decode to an object")
        return CharacterStateSnapshot(
            version=int(row["version"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            values=values,
        )
