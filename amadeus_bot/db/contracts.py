from __future__ import annotations

from typing import Protocol

from amadeus_bot.character import CharacterStateSnapshot
from amadeus_bot.memory import MemoryRecord
from amadeus_bot.runtime import RuntimeEvent


class EventStore(Protocol):
    async def append(self, event: RuntimeEvent) -> None:
        ...


class MemoryStore(Protocol):
    async def get(self, memory_id: str) -> MemoryRecord | None:
        ...

    async def upsert(self, memory: MemoryRecord) -> None:
        ...

    async def retrieve(self, query: str, *, limit: int) -> tuple[MemoryRecord, ...]:
        ...


class CharacterStateStore(Protocol):
    async def load(self) -> CharacterStateSnapshot | None:
        ...

    async def save(self, state: CharacterStateSnapshot) -> None:
        ...
