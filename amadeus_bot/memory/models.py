from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MemoryKind(StrEnum):
    EPISODE = "episode"
    FACT = "fact"
    PREFERENCE = "preference"
    RELATIONSHIP = "relationship"
    IMPRESSION = "impression"
    SELF_MEMORY = "self_memory"
    OPEN_THREAD = "open_thread"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    FORGOTTEN = "forgotten"


class MemorySourceType(StrEnum):
    EXPLICIT_USER = "explicit_user"
    ARCHIVIST = "archivist"
    CHARACTER_INFERENCE = "character_inference"
    MANUAL = "manual"
    MIGRATED = "migrated"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """Structured long-term memory with provenance, correction, and decay metadata."""

    memory_id: str
    kind: MemoryKind
    content: str
    confidence: float
    salience: float
    created_at: datetime
    source_message_ids: tuple[int, ...]
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_type: MemorySourceType = MemorySourceType.ARCHIVIST
    updated_at: datetime | None = None
    last_recalled_at: datetime | None = None
    expires_at: datetime | None = None
    supersedes_id: str | None = None
    contradicts_id: str | None = None
    tags: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.memory_id.strip():
            raise ValueError("memory_id must not be empty")
        if not self.content.strip():
            raise ValueError("memory content must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("memory confidence must be between 0 and 1")
        if not 0.0 <= self.salience <= 1.0:
            raise ValueError("memory salience must be between 0 and 1")
        self._validate_datetime("created_at", self.created_at)
        for datetime_name, datetime_value in (
            ("updated_at", self.updated_at),
            ("last_recalled_at", self.last_recalled_at),
            ("expires_at", self.expires_at),
        ):
            if datetime_value is not None:
                self._validate_datetime(datetime_name, datetime_value)
        if any(message_id <= 0 for message_id in self.source_message_ids):
            raise ValueError("source_message_ids must contain only positive IDs")
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("source_message_ids must not contain duplicates")
        provenance_required = {
            MemorySourceType.EXPLICIT_USER,
            MemorySourceType.ARCHIVIST,
            MemorySourceType.CHARACTER_INFERENCE,
        }
        if self.source_type in provenance_required and not self.source_message_ids:
            raise ValueError("semantic memory sources require source_message_ids provenance")
        for link_name, link_value in (
            ("supersedes_id", self.supersedes_id),
            ("contradicts_id", self.contradicts_id),
        ):
            if link_value is not None and not link_value.strip():
                raise ValueError(f"{link_name} must not be blank")
        if self.supersedes_id == self.memory_id or self.contradicts_id == self.memory_id:
            raise ValueError("memory cannot supersede or contradict itself")
        if any(not item.strip() for item in (*self.tags, *self.entities)):
            raise ValueError("memory tags/entities must not contain blank values")
        if (
            self.kind is MemoryKind.FACT
            and self.source_type is MemorySourceType.CHARACTER_INFERENCE
        ):
            raise ValueError("character inference cannot be stored as confirmed fact")

    @property
    def effective_updated_at(self) -> datetime:
        return self.updated_at or self.created_at

    def is_active(self, *, at: datetime | None = None) -> bool:
        if self.status is not MemoryStatus.ACTIVE:
            return False
        if self.expires_at is None:
            return True
        reference = at or datetime.now(self.expires_at.tzinfo)
        self._validate_datetime("at", reference)
        return self.expires_at > reference

    @staticmethod
    def _validate_datetime(name: str, value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
