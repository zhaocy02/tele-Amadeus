import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amadeus_bot.character import (
    CharacterState,
    SQLiteCharacterStateStore,
    StateResidue,
)


def _at(hour: int = 12) -> datetime:
    return datetime(2026, 9, 2, hour, 0, tzinfo=UTC)


def test_sqlite_state_store_round_trips_and_keeps_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        try:
            first = CharacterState.initial(at=_at())
            second = CharacterState(
                state_version=2,
                updated_at=_at(13),
                relationship_tone="familiar_teasing",
                emotional_stance=StateResidue(
                    text="mildly annoyed",
                    expires_at=_at(13) + timedelta(hours=2),
                ),
            )

            await store.save_state(first)
            await store.save_state(second)

            loaded = await store.load_state()
            history = await store.history()

            assert loaded == second
            assert tuple(snapshot.version for snapshot in history) == (2, 1)
            assert CharacterState.from_snapshot(history[0]) == second
        finally:
            store.close()

    asyncio.run(scenario())


def test_state_store_rejects_non_monotonic_versions(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        try:
            await store.save_state(CharacterState.initial(at=_at()))
            with pytest.raises(ValueError, match="expected 2"):
                await store.save_state(CharacterState.initial(at=_at(13)))
        finally:
            store.close()

    asyncio.run(scenario())


def test_state_store_loads_none_before_first_save(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        try:
            assert await store.load() is None
            assert await store.load_state() is None
        finally:
            store.close()

    asyncio.run(scenario())
