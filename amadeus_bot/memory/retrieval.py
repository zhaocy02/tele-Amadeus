from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .models import MemoryKind, MemoryRecord
from .structured_store import StructuredMemoryRepository

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")


@dataclass(frozen=True, slots=True)
class MemoryScoreBreakdown:
    lexical: float
    recency: float
    salience: float
    confidence: float
    type_bonus: float
    total: float


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory: MemoryRecord
    score: MemoryScoreBreakdown


@dataclass(frozen=True, slots=True)
class MemoryRetrievalResult:
    items: tuple[RetrievedMemory, ...] = ()

    @property
    def memory_ids(self) -> tuple[str, ...]:
        return tuple(item.memory.memory_id for item in self.items)

    @property
    def confirmed_facts(self) -> tuple[str, ...]:
        return tuple(
            self._render(item.memory)
            for item in self.items
            if item.memory.kind in {MemoryKind.FACT, MemoryKind.PREFERENCE}
        )

    @property
    def relationship_memories(self) -> tuple[str, ...]:
        return tuple(
            self._render(item.memory)
            for item in self.items
            if item.memory.kind
            in {MemoryKind.RELATIONSHIP, MemoryKind.EPISODE, MemoryKind.SELF_MEMORY}
        )

    @property
    def character_impressions(self) -> tuple[str, ...]:
        return tuple(
            self._render(item.memory)
            for item in self.items
            if item.memory.kind is MemoryKind.IMPRESSION
        )

    @property
    def open_threads(self) -> tuple[str, ...]:
        return tuple(
            self._render(item.memory)
            for item in self.items
            if item.memory.kind is MemoryKind.OPEN_THREAD
        )

    @staticmethod
    def _render(memory: MemoryRecord) -> str:
        return f"[{memory.kind.value}] {memory.content}"


class MemoryRetriever:
    """Retrieve a small relevant memory set using transparent non-embedding scoring."""

    _GROUP_QUOTAS = {
        "fact": 2,
        "relationship": 2,
        "impression": 2,
        "self": 1,
        "open_thread": 2,
    }

    def __init__(
        self,
        repository: StructuredMemoryRepository,
        *,
        candidate_limit: int = 200,
        min_score: float = 0.18,
    ) -> None:
        if candidate_limit <= 0:
            raise ValueError("candidate_limit must be positive")
        if not 0.0 <= min_score <= 1.0:
            raise ValueError("min_score must be between 0 and 1")
        self._repository = repository
        self._candidate_limit = candidate_limit
        self._min_score = min_score

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 7,
        at: datetime | None = None,
        mark_recalled: bool = False,
    ) -> MemoryRetrievalResult:
        text = query.strip()
        if not text:
            return MemoryRetrievalResult()
        if limit <= 0:
            return MemoryRetrievalResult()
        safe_limit = min(limit, 12)
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        query_terms = self._terms(text)
        candidates = await self._repository.list_active(
            limit=self._candidate_limit,
            at=timestamp,
        )

        scored: list[RetrievedMemory] = []
        for memory in candidates:
            breakdown = self._score(memory, query_terms=query_terms, at=timestamp)
            if breakdown.total < self._min_score:
                continue
            if not self._passes_relevance_gate(memory, breakdown):
                continue
            scored.append(RetrievedMemory(memory=memory, score=breakdown))

        scored.sort(
            key=lambda item: (
                item.score.total,
                item.memory.salience,
                item.memory.confidence,
                item.memory.effective_updated_at.timestamp(),
            ),
            reverse=True,
        )
        selected = self._apply_quotas(scored, safe_limit)
        result = MemoryRetrievalResult(items=tuple(selected))
        if mark_recalled and result.memory_ids:
            await self._repository.touch_recalled(result.memory_ids, at=timestamp)
        return result

    def _score(
        self,
        memory: MemoryRecord,
        *,
        query_terms: frozenset[str],
        at: datetime,
    ) -> MemoryScoreBreakdown:
        memory_terms = self._terms(
            " ".join((memory.content, *memory.tags, *memory.entities))
        )
        lexical = self._lexical_overlap(query_terms, memory_terms)
        age_seconds = max(0.0, (at - memory.effective_updated_at).total_seconds())
        age_days = age_seconds / 86400.0
        recency = math.exp(-age_days / 30.0)
        type_bonus = self._type_bonus(memory.kind)
        total = min(
            1.0,
            0.55 * lexical
            + 0.12 * recency
            + 0.17 * memory.salience
            + 0.10 * memory.confidence
            + type_bonus,
        )
        return MemoryScoreBreakdown(
            lexical=lexical,
            recency=recency,
            salience=memory.salience,
            confidence=memory.confidence,
            type_bonus=type_bonus,
            total=total,
        )

    @staticmethod
    def _passes_relevance_gate(
        memory: MemoryRecord,
        score: MemoryScoreBreakdown,
    ) -> bool:
        if score.lexical > 0.0:
            return True
        if (
            memory.kind is MemoryKind.OPEN_THREAD
            and memory.salience >= 0.85
            and score.recency >= 0.80
        ):
            return True
        return (
            memory.kind is MemoryKind.RELATIONSHIP
            and memory.salience >= 0.95
            and score.recency >= 0.85
        )

    @classmethod
    def _apply_quotas(
        cls,
        candidates: list[RetrievedMemory],
        limit: int,
    ) -> list[RetrievedMemory]:
        selected: list[RetrievedMemory] = []
        counts = {group: 0 for group in cls._GROUP_QUOTAS}
        for item in candidates:
            group = cls._quota_group(item.memory.kind)
            if counts[group] >= cls._GROUP_QUOTAS[group]:
                continue
            selected.append(item)
            counts[group] += 1
            if len(selected) >= limit:
                break
        return selected

    @staticmethod
    def _quota_group(kind: MemoryKind) -> str:
        if kind in {MemoryKind.FACT, MemoryKind.PREFERENCE}:
            return "fact"
        if kind in {MemoryKind.RELATIONSHIP, MemoryKind.EPISODE}:
            return "relationship"
        if kind is MemoryKind.IMPRESSION:
            return "impression"
        if kind is MemoryKind.SELF_MEMORY:
            return "self"
        return "open_thread"

    @staticmethod
    def _type_bonus(kind: MemoryKind) -> float:
        return {
            MemoryKind.OPEN_THREAD: 0.10,
            MemoryKind.RELATIONSHIP: 0.08,
            MemoryKind.PREFERENCE: 0.06,
            MemoryKind.FACT: 0.05,
            MemoryKind.SELF_MEMORY: 0.05,
            MemoryKind.EPISODE: 0.04,
            MemoryKind.IMPRESSION: 0.02,
        }[kind]

    @staticmethod
    def _lexical_overlap(left: frozenset[str], right: frozenset[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / max(1, min(len(left), len(right)))

    @staticmethod
    def _terms(text: str) -> frozenset[str]:
        terms: set[str] = set()
        for match in _TOKEN_RE.finditer(text.casefold()):
            token = match.group(0)
            terms.add(token)
            if _CJK_RE.fullmatch(token) and len(token) > 1:
                terms.update(token[index : index + 2] for index in range(len(token) - 1))
        return frozenset(terms)

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieval timestamp must be timezone-aware")
