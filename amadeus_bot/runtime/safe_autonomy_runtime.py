from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from amadeus_bot.character import AutonomySignal, AutonomySignalKind, CharacterState
from amadeus_bot.memory import MemoryKind, MemoryRecord

from .autonomy_runtime import AutonomyRuntimeCoordinator as _BaseAutonomyRuntimeCoordinator

_SIGNAL_SUMMARY_MAX = 500
_SIGNAL_ID_MAX = 120
_SOURCE_ID_MAX = 160
_MEMORY_CANDIDATE_LIMIT = 24
_MEMORY_SIGNAL_LIMIT = 12
_MEMORY_FRESHNESS_WINDOW = timedelta(days=30)
_MEMORY_FRESHNESS_FLOOR = 0.65
_MEMORY_KIND_FACTOR = {
    MemoryKind.OPEN_THREAD: 1.00,
    MemoryKind.RELATIONSHIP: 0.95,
    MemoryKind.SELF_MEMORY: 0.90,
    MemoryKind.EPISODE: 0.85,
    MemoryKind.PREFERENCE: 0.80,
}


class AutonomyRuntimeCoordinator(_BaseAutonomyRuntimeCoordinator):
    """Runtime coordinator that safely adapts and ranks memories for proactive use."""

    async def _build_signals(
        self,
        chat_id: int,
        state: CharacterState,
        *,
        at: datetime,
    ) -> tuple[AutonomySignal, ...]:
        memories = await self._memory_repository.list_active(
            kinds=(
                MemoryKind.OPEN_THREAD,
                MemoryKind.RELATIONSHIP,
                MemoryKind.EPISODE,
                MemoryKind.PREFERENCE,
                MemoryKind.SELF_MEMORY,
            ),
            limit=_MEMORY_CANDIDATE_LIMIT,
            at=at,
        )
        memory_signals = [self._memory_signal_at(memory, at=at) for memory in memories]
        memory_signals.sort(key=lambda signal: signal.salience, reverse=True)
        signals: list[AutonomySignal] = memory_signals[:_MEMORY_SIGNAL_LIMIT]

        state_residues = (*state.current_preoccupations, *state.unresolved_feelings)
        for index, residue in enumerate(state_residues[:6]):
            signals.append(self._state_signal(state, residue, index=index, at=at))
        signals.extend(self._recent_conversation_signals(chat_id, at=at, limit=4))
        return tuple(signals[:24])

    @staticmethod
    def _memory_signal(memory: MemoryRecord) -> AutonomySignal:
        """Compatibility helper for direct callers; live runtime uses the at-aware variant."""

        return _memory_signal(memory, salience=memory.salience)

    @staticmethod
    def _memory_signal_at(memory: MemoryRecord, *, at: datetime) -> AutonomySignal:
        return _memory_signal(memory, salience=_effective_memory_salience(memory, at=at))


def _memory_signal(memory: MemoryRecord, *, salience: float) -> AutonomySignal:
    summary = _bounded_summary(memory.content)
    source_id = _bounded_identifier(memory.memory_id, _SOURCE_ID_MAX)
    if memory.kind is MemoryKind.OPEN_THREAD:
        return AutonomySignal(
            signal_id=_bounded_signal_id("open-thread", memory.memory_id),
            kind=AutonomySignalKind.OPEN_THREAD,
            summary=summary,
            salience=salience,
            source_thread_id=source_id,
        )
    kind = (
        AutonomySignalKind.RELATIONSHIP_MEMORY
        if memory.kind is MemoryKind.RELATIONSHIP
        else AutonomySignalKind.MEMORY
    )
    return AutonomySignal(
        signal_id=_bounded_signal_id("memory", memory.memory_id),
        kind=kind,
        summary=summary,
        salience=salience,
        source_memory_id=source_id,
    )


def _effective_memory_salience(memory: MemoryRecord, *, at: datetime) -> float:
    """Down-rank stale/recently reused memories without erasing their intrinsic importance."""

    age = max(timedelta(0), at - memory.effective_updated_at)
    freshness_progress = min(1.0, age / _MEMORY_FRESHNESS_WINDOW)
    freshness = 1.0 - (1.0 - _MEMORY_FRESHNESS_FLOOR) * freshness_progress
    kind_factor = _MEMORY_KIND_FACTOR.get(memory.kind, 0.85)
    recall_factor = _recent_recall_factor(memory, at=at)
    return _clamp(memory.salience * kind_factor * freshness * recall_factor)


def _recent_recall_factor(memory: MemoryRecord, *, at: datetime) -> float:
    recalled = memory.last_recalled_at
    if recalled is None:
        return 1.0
    elapsed = max(timedelta(0), at - recalled)
    if elapsed <= timedelta(hours=6):
        return 0.72
    if elapsed <= timedelta(hours=24):
        return 0.82
    if elapsed <= timedelta(hours=72):
        return 0.92
    return 1.0


def _bounded_summary(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) <= _SIGNAL_SUMMARY_MAX:
        return compact
    return compact[: _SIGNAL_SUMMARY_MAX - 1].rstrip() + "…"


def _bounded_signal_id(prefix: str, memory_id: str) -> str:
    value = f"{prefix}:{memory_id.strip()}"
    if len(value) <= _SIGNAL_ID_MAX:
        return value
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=12).hexdigest()
    available = _SIGNAL_ID_MAX - len(prefix) - len(digest) - 2
    stem = memory_id.strip()[: max(1, available)].rstrip()
    return f"{prefix}:{stem}:{digest}"


def _bounded_identifier(value: str, limit: int) -> str:
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.blake2s(cleaned.encode("utf-8"), digest_size=12).hexdigest()
    stem = cleaned[: limit - len(digest) - 1].rstrip()
    return f"{stem}:{digest}"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, round(value, 4)))
