from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from amadeus_bot.llm import LLMMessage, LLMProvider, LLMRequest, MessageRole

from .legacy_store import is_sensitive_memory
from .models import MemoryKind, MemoryRecord, MemorySourceType, MemoryStatus
from .structured_store import StructuredMemoryRepository

ARCHIVIST_PROMPT_VERSION = "memory-archivist-v1"
_MAX_CANDIDATE_MEMORIES = 12


class ArchivistAction(StrEnum):
    NO_WRITE = "NO_WRITE"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    SUPERSEDE = "SUPERSEDE"
    REJECT = "REJECT"


class ArchivistEvidence(StrEnum):
    EXPLICIT_USER = "explicit_user"
    CONVERSATION_EVENT = "conversation_event"
    CHARACTER_INFERENCE = "character_inference"
    ASSISTANT_SELF = "assistant_self"


class ArchivistItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MemoryKind
    content: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
    salience: float = Field(ge=0.0, le=1.0)
    source_message_ids: tuple[int, ...]
    evidence: ArchivistEvidence
    target_memory_id: str | None = None
    expires_at: datetime | None = None
    tags: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()


class ArchivistDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ArchivistAction
    items: tuple[ArchivistItem, ...] = ()
    reason_label: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> ArchivistDecision:
        if self.decision is ArchivistAction.NO_WRITE and self.items:
            raise ValueError("NO_WRITE must not contain memory items")
        if self.decision is not ArchivistAction.NO_WRITE and not self.items:
            raise ValueError("memory mutation decision requires at least one item")
        return self


@dataclass(frozen=True, slots=True)
class ArchivistContext:
    user_message_id: int
    assistant_message_id: int
    user_text: str
    assistant_text: str
    candidate_memories: tuple[MemoryRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.user_message_id <= 0 or self.assistant_message_id <= 0:
            raise ValueError("Archivist source message IDs must be positive")
        if self.user_message_id == self.assistant_message_id:
            raise ValueError("user and assistant message IDs must differ")
        if not self.user_text.strip() or not self.assistant_text.strip():
            raise ValueError("Archivist turn text must not be empty")
        memory_ids = tuple(memory.memory_id for memory in self.candidate_memories)
        if len(set(memory_ids)) != len(memory_ids):
            raise ValueError("Archivist candidate memory IDs must be unique")

    @property
    def allowed_source_message_ids(self) -> frozenset[int]:
        return frozenset((self.user_message_id, self.assistant_message_id))

    @property
    def exposed_candidate_memories(self) -> tuple[MemoryRecord, ...]:
        return self.candidate_memories[:_MAX_CANDIDATE_MEMORIES]

    @property
    def allowed_target_memory_ids(self) -> frozenset[str]:
        return frozenset(memory.memory_id for memory in self.exposed_candidate_memories)


@dataclass(frozen=True, slots=True)
class ArchivistApplyResult:
    written_records: tuple[MemoryRecord, ...] = ()
    superseded_memory_ids: tuple[str, ...] = ()
    rejected_memory_ids: tuple[str, ...] = ()


class MemoryArchivist:
    """Propose and safely apply sparse long-term memory mutations after a turn."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._id_factory = id_factory or self._default_id

    async def analyze(self, context: ArchivistContext) -> ArchivistDecision:
        request = LLMRequest(
            messages=(
                LLMMessage(MessageRole.SYSTEM, self._system_prompt()),
                LLMMessage(MessageRole.USER, self._context_prompt(context)),
            ),
            model=self._model,
            metadata={"prompt_version": ARCHIVIST_PROMPT_VERSION},
        )
        try:
            response = await self._provider.generate(request)
            decision = ArchivistDecision.model_validate_json(response.text)
        except (ValidationError, ValueError, RuntimeError):
            return self._no_write("archivist_failure")
        return self._normalize(decision, context)

    async def process(
        self,
        *,
        context: ArchivistContext,
        repository: StructuredMemoryRepository,
        at: datetime | None = None,
    ) -> ArchivistApplyResult:
        decision = await self.analyze(context)
        return await self.apply(
            decision=decision,
            context=context,
            repository=repository,
            at=at,
        )

    async def apply(
        self,
        *,
        decision: ArchivistDecision,
        context: ArchivistContext,
        repository: StructuredMemoryRepository,
        at: datetime | None = None,
    ) -> ArchivistApplyResult:
        normalized = self._normalize(decision, context)
        if normalized.decision is ArchivistAction.NO_WRITE:
            return ArchivistApplyResult()

        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        written: list[MemoryRecord] = []
        superseded: list[str] = []
        rejected: list[str] = []

        for item in normalized.items:
            if normalized.decision is ArchivistAction.CREATE:
                record = await self._record_from_item(item, repository, timestamp)
                await repository.upsert(record)
                written.append(record)
                continue

            target_id = item.target_memory_id
            if target_id is None:
                continue
            existing = await repository.get(target_id)
            if existing is None:
                continue

            if normalized.decision is ArchivistAction.REJECT:
                changed = await repository.set_status(
                    target_id,
                    MemoryStatus.REJECTED,
                    at=timestamp,
                    detail="archivist_reject",
                )
                if changed:
                    rejected.append(target_id)
                continue

            replacement = await self._record_from_item(item, repository, timestamp)
            saved = await repository.supersede(target_id, replacement, at=timestamp)
            written.append(saved)
            superseded.append(target_id)

        return ArchivistApplyResult(
            written_records=tuple(written),
            superseded_memory_ids=tuple(superseded),
            rejected_memory_ids=tuple(rejected),
        )

    def _normalize(
        self,
        decision: ArchivistDecision,
        context: ArchivistContext,
    ) -> ArchivistDecision:
        if decision.decision is ArchivistAction.NO_WRITE:
            return decision

        allowed_sources = context.allowed_source_message_ids
        allowed_targets = context.allowed_target_memory_ids
        valid_items: list[ArchivistItem] = []
        for item in decision.items:
            source_ids = frozenset(item.source_message_ids)
            if not source_ids or not source_ids.issubset(allowed_sources):
                continue
            if len(source_ids) != len(item.source_message_ids):
                continue
            if not self._evidence_matches_sources(item, context):
                continue
            if not self._evidence_matches_kind(item):
                continue
            if self._contains_sensitive_payload(item):
                continue
            if decision.decision is ArchivistAction.CREATE:
                if item.target_memory_id is not None:
                    continue
            else:
                target_id = item.target_memory_id
                if not target_id or target_id not in allowed_targets:
                    continue
            try:
                self._validation_record(item)
            except ValueError:
                continue
            valid_items.append(item)

        if not valid_items:
            return self._no_write("archivist_items_rejected")
        return decision.model_copy(update={"items": tuple(valid_items)})

    @staticmethod
    def _contains_sensitive_payload(item: ArchivistItem) -> bool:
        payload = " ".join((item.content, *item.tags, *item.entities))
        return is_sensitive_memory(payload)

    @staticmethod
    def _evidence_matches_sources(item: ArchivistItem, context: ArchivistContext) -> bool:
        source_ids = frozenset(item.source_message_ids)
        if item.evidence is ArchivistEvidence.EXPLICIT_USER:
            return source_ids == frozenset((context.user_message_id,))
        if item.evidence is ArchivistEvidence.ASSISTANT_SELF:
            return source_ids == frozenset((context.assistant_message_id,))
        return True

    @staticmethod
    def _evidence_matches_kind(item: ArchivistItem) -> bool:
        if item.evidence is ArchivistEvidence.CHARACTER_INFERENCE:
            return item.kind is MemoryKind.IMPRESSION
        if item.evidence is ArchivistEvidence.ASSISTANT_SELF:
            return item.kind is MemoryKind.SELF_MEMORY
        if item.evidence is ArchivistEvidence.CONVERSATION_EVENT:
            return item.kind in {
                MemoryKind.EPISODE,
                MemoryKind.RELATIONSHIP,
                MemoryKind.SELF_MEMORY,
                MemoryKind.OPEN_THREAD,
            }
        if item.kind in {MemoryKind.FACT, MemoryKind.PREFERENCE}:
            return item.evidence is ArchivistEvidence.EXPLICIT_USER
        return item.kind is not MemoryKind.SELF_MEMORY

    def _validation_record(self, item: ArchivistItem) -> MemoryRecord:
        return MemoryRecord(
            memory_id="mem_validation",
            kind=item.kind,
            content=item.content.strip(),
            confidence=item.confidence,
            salience=item.salience,
            created_at=datetime.now(UTC),
            source_message_ids=item.source_message_ids,
            source_type=self._source_type(item.evidence),
            expires_at=item.expires_at,
            tags=tuple(tag.strip() for tag in item.tags if tag.strip()),
            entities=tuple(entity.strip() for entity in item.entities if entity.strip()),
        )

    async def _record_from_item(
        self,
        item: ArchivistItem,
        repository: StructuredMemoryRepository,
        created_at: datetime,
    ) -> MemoryRecord:
        memory_id = await self._new_memory_id(repository)
        return MemoryRecord(
            memory_id=memory_id,
            kind=item.kind,
            content=item.content.strip(),
            confidence=item.confidence,
            salience=item.salience,
            created_at=created_at,
            source_message_ids=item.source_message_ids,
            source_type=self._source_type(item.evidence),
            updated_at=created_at,
            expires_at=item.expires_at,
            tags=tuple(tag.strip() for tag in item.tags if tag.strip()),
            entities=tuple(entity.strip() for entity in item.entities if entity.strip()),
        )

    async def _new_memory_id(self, repository: StructuredMemoryRepository) -> str:
        for _ in range(5):
            candidate = self._id_factory().strip()
            if candidate and await repository.get(candidate) is None:
                return candidate
        raise RuntimeError("could not allocate a unique memory ID")

    @staticmethod
    def _source_type(evidence: ArchivistEvidence) -> MemorySourceType:
        if evidence is ArchivistEvidence.EXPLICIT_USER:
            return MemorySourceType.EXPLICIT_USER
        if evidence is ArchivistEvidence.CHARACTER_INFERENCE:
            return MemorySourceType.CHARACTER_INFERENCE
        return MemorySourceType.ARCHIVIST

    @staticmethod
    def _no_write(reason: str) -> ArchivistDecision:
        return ArchivistDecision(
            decision=ArchivistAction.NO_WRITE,
            reason_label=reason,
        )

    @staticmethod
    def _default_id() -> str:
        return "mem_" + uuid4().hex

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Archivist mutation timestamps must be timezone-aware")

    @staticmethod
    def _system_prompt() -> str:
        kinds = "|".join(kind.value for kind in MemoryKind)
        evidence = "|".join(item.value for item in ArchivistEvidence)
        actions = "|".join(action.value for action in ArchivistAction)
        return "\n".join(
            (
                "You are the Memory Archivist for a persistent Kurisu character system.",
                "You do not perform as Kurisu and do not write user-visible prose.",
                "Prefer NO_WRITE. Ordinary small talk should usually produce NO_WRITE.",
                "Never convert inference into confirmed fact or preference.",
                "Facts and preferences require explicit user evidence.",
                "Character inference may create impression memory only.",
                "Assistant-self evidence may create self_memory only.",
                (
                    "Conversation-event evidence is for episodes, relationships, "
                    "self history, or threads."
                ),
                "Do not save passwords, tokens, credentials, verification codes, or private keys.",
                "Use only source message IDs supplied in the input. Never invent message IDs.",
                "Only target candidate memory IDs supplied in the input for mutation.",
                "For corrections, prefer SUPERSEDE or REJECT over erasing history.",
                "Return exactly one JSON object and no markdown.",
                "Schema:",
                "{",
                f'  "decision": "{actions}",',
                '  "items": [',
                "    {",
                f'      "kind": "{kinds}",',
                '      "content": "...",',
                '      "confidence": 0.0,',
                '      "salience": 0.0,',
                '      "source_message_ids": [1],',
                f'      "evidence": "{evidence}",',
                '      "target_memory_id": null,',
                '      "expires_at": null,',
                '      "tags": [],',
                '      "entities": []',
                "    }",
                "  ],",
                '  "reason_label": ""',
                "}",
            )
        )

    @staticmethod
    def _context_prompt(context: ArchivistContext) -> str:
        memories = [
            {
                "memory_id": memory.memory_id,
                "kind": memory.kind.value,
                "content": memory.content,
                "status": memory.status.value,
                "confidence": memory.confidence,
            }
            for memory in context.exposed_candidate_memories
        ]
        payload = {
            "allowed_source_message_ids": [
                context.user_message_id,
                context.assistant_message_id,
            ],
            "turn": {
                "user": {
                    "message_id": context.user_message_id,
                    "text": context.user_text,
                },
                "assistant": {
                    "message_id": context.assistant_message_id,
                    "text": context.assistant_text,
                },
            },
            "candidate_existing_memories": memories,
        }
        return "[ARCHIVIST INPUT — DATA]\n" + json.dumps(payload, ensure_ascii=False, indent=2)
