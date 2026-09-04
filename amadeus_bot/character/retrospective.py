from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from amadeus_bot.llm import LLMMessage, LLMProvider, LLMRequest, MessageRole
from amadeus_bot.memory import MemoryRecord

from .state import (
    AssumptionStatus,
    CharacterState,
    MistakeScope,
    PlausibleMistake,
    StateResidue,
    WorkingAssumption,
)

RETROSPECTIVE_PROMPT_VERSION = "character-retrospective-v1"
_MAX_MESSAGES = 30
_MAX_MEMORIES = 12


class RetrospectiveAction(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    UPDATE = "UPDATE"


class RetrospectiveResidueProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    ttl_hours: int = Field(ge=1, le=168)


class RetrospectiveAssumptionProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)
    status: AssumptionStatus = AssumptionStatus.ACTIVE
    source_message_ids: tuple[int, ...] = ()
    source_memory_ids: tuple[str, ...] = ()
    ttl_hours: int = Field(default=168, ge=1, le=336)


class RetrospectiveMistakeProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=400)
    confidence: float = Field(ge=0.0, le=1.0)
    scope: MistakeScope = MistakeScope.CASUAL_ONLY
    ttl_turns: int = Field(default=6, ge=1, le=20)
    source_message_ids: tuple[int, ...] = ()
    source_memory_ids: tuple[str, ...] = ()
    ttl_hours: int = Field(default=24, ge=1, le=72)


class RetrospectiveDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: RetrospectiveAction
    emotional_stance: RetrospectiveResidueProposal | None = None
    relationship_tone: str | None = Field(default=None, max_length=200)
    current_preoccupations: tuple[RetrospectiveResidueProposal, ...] | None = None
    working_assumptions: tuple[RetrospectiveAssumptionProposal, ...] | None = None
    unresolved_feelings: tuple[RetrospectiveResidueProposal, ...] | None = None
    plausible_mistakes: tuple[RetrospectiveMistakeProposal, ...] | None = None
    next_reaction_bias: RetrospectiveResidueProposal | None = None
    avoid_sounding_like: tuple[str, ...] | None = None
    open_thread_ids: tuple[str, ...] | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason_label: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> RetrospectiveDecision:
        update_fields = (
            self.emotional_stance,
            self.relationship_tone,
            self.current_preoccupations,
            self.working_assumptions,
            self.unresolved_feelings,
            self.plausible_mistakes,
            self.next_reaction_bias,
            self.avoid_sounding_like,
            self.open_thread_ids,
        )
        has_update = any(value is not None for value in update_fields)
        if self.decision is RetrospectiveAction.NO_CHANGE and has_update:
            raise ValueError("NO_CHANGE must not contain state updates")
        if self.decision is RetrospectiveAction.UPDATE and not has_update:
            raise ValueError("UPDATE requires at least one state field")
        return self


@dataclass(frozen=True, slots=True)
class RetrospectiveMessage:
    message_id: int
    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if self.message_id <= 0:
            raise ValueError("retrospective message_id must be positive")
        if self.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
            raise ValueError("retrospective messages may contain only user/assistant roles")
        if not self.content.strip():
            raise ValueError("retrospective message content must not be empty")


@dataclass(frozen=True, slots=True)
class RetrospectiveContext:
    current_state: CharacterState
    recent_messages: tuple[RetrospectiveMessage, ...] = ()
    relevant_memories: tuple[MemoryRecord, ...] = ()
    recent_acts: tuple[str, ...] = ()
    available_open_thread_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        message_ids = tuple(item.message_id for item in self.recent_messages)
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("retrospective message IDs must be unique")
        memory_ids = tuple(item.memory_id for item in self.relevant_memories)
        if len(set(memory_ids)) != len(memory_ids):
            raise ValueError("retrospective memory IDs must be unique")
        cleaned_threads = tuple(item.strip() for item in self.available_open_thread_ids)
        if any(not item for item in cleaned_threads):
            raise ValueError("available open thread IDs must not be blank")
        if len(set(cleaned_threads)) != len(cleaned_threads):
            raise ValueError("available open thread IDs must be unique")

    @property
    def exposed_messages(self) -> tuple[RetrospectiveMessage, ...]:
        return self.recent_messages[-_MAX_MESSAGES:]

    @property
    def exposed_memories(self) -> tuple[MemoryRecord, ...]:
        return self.relevant_memories[:_MAX_MEMORIES]

    @property
    def allowed_message_ids(self) -> frozenset[int]:
        return frozenset(item.message_id for item in self.exposed_messages)

    @property
    def allowed_memory_ids(self) -> frozenset[str]:
        return frozenset(item.memory_id for item in self.exposed_memories)

    @property
    def allowed_open_thread_ids(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.available_open_thread_ids)


@dataclass(frozen=True, slots=True)
class RetrospectiveResult:
    state: CharacterState
    changed: bool
    reason_label: str


@dataclass(frozen=True, slots=True)
class RetrospectiveTriggerContext:
    turns_since_last: int
    correction_detected: bool = False
    relationship_event: bool = False
    user_character_feedback: bool = False
    repeated_pattern_detected: bool = False
    autonomy_feedback: bool = False

    def __post_init__(self) -> None:
        if self.turns_since_last < 0:
            raise ValueError("turns_since_last must not be negative")


class RetrospectiveTriggerPolicy:
    """Deterministic low-frequency gate for the hidden retrospective call."""

    def __init__(self, *, interval_turns: int = 16) -> None:
        if interval_turns < 2:
            raise ValueError("retrospective interval_turns must be >= 2")
        self._interval_turns = interval_turns

    def should_run(self, context: RetrospectiveTriggerContext) -> bool:
        return context.turns_since_last >= self._interval_turns or any(
            (
                context.correction_detected,
                context.relationship_event,
                context.user_character_feedback,
                context.repeated_pattern_detected,
                context.autonomy_feedback,
            )
        )


class CharacterRetrospective:
    """Review interaction history and update subjective Character State only."""

    def __init__(self, *, provider: LLMProvider, model: str | None = None) -> None:
        self._provider = provider
        self._model = model

    async def review(
        self,
        context: RetrospectiveContext,
        *,
        at: datetime,
    ) -> RetrospectiveResult:
        self._validate_aware(at)
        request = LLMRequest(
            messages=(
                LLMMessage(MessageRole.SYSTEM, self._system_prompt()),
                LLMMessage(MessageRole.USER, self._context_prompt(context)),
            ),
            model=self._model,
            metadata={"prompt_version": RETROSPECTIVE_PROMPT_VERSION},
        )
        try:
            response = await self._provider.generate(request)
            decision = RetrospectiveDecision.model_validate_json(response.text)
        except (ValidationError, ValueError, RuntimeError):
            return RetrospectiveResult(
                state=context.current_state,
                changed=False,
                reason_label="retrospective_failure",
            )

        if decision.decision is RetrospectiveAction.NO_CHANGE:
            return RetrospectiveResult(
                state=context.current_state,
                changed=False,
                reason_label=decision.reason_label or "retrospective_no_change",
            )
        return self._apply(decision, context, at=at)

    def _apply(
        self,
        decision: RetrospectiveDecision,
        context: RetrospectiveContext,
        *,
        at: datetime,
    ) -> RetrospectiveResult:
        current = context.current_state.effective(at=at)
        updates: dict[str, object] = {}

        if decision.emotional_stance is not None:
            updates["emotional_stance"] = self._residue(
                decision.emotional_stance,
                at=at,
                max_hours=24,
            )
        if decision.relationship_tone is not None:
            relationship = decision.relationship_tone.strip()
            if relationship:
                updates["relationship_tone"] = relationship
        if decision.current_preoccupations is not None:
            updates["current_preoccupations"] = tuple(
                self._residue(item, at=at, max_hours=168)
                for item in decision.current_preoccupations[:8]
            )

        assumptions = self._normalize_assumptions(
            decision.working_assumptions,
            context=context,
            at=at,
        )
        if assumptions is not None:
            updates["working_assumptions"] = assumptions

        if decision.unresolved_feelings is not None:
            updates["unresolved_feelings"] = tuple(
                self._residue(item, at=at, max_hours=48)
                for item in decision.unresolved_feelings[:6]
            )

        mistakes = self._normalize_mistakes(
            decision.plausible_mistakes,
            context=context,
            at=at,
        )
        if mistakes is not None:
            updates["plausible_mistakes"] = mistakes

        if decision.next_reaction_bias is not None:
            updates["next_reaction_bias"] = self._residue(
                decision.next_reaction_bias,
                at=at,
                max_hours=72,
            )
        if decision.avoid_sounding_like is not None:
            updates["avoid_sounding_like"] = self._clean_strings(
                decision.avoid_sounding_like,
                limit=8,
            )

        threads = self._normalize_open_threads(decision.open_thread_ids, context)
        if threads is not None:
            updates["open_thread_ids"] = threads

        if not updates:
            return RetrospectiveResult(
                state=context.current_state,
                changed=False,
                reason_label="retrospective_items_rejected",
            )

        candidate = current.model_copy(
            update={
                **updates,
                "state_version": context.current_state.state_version + 1,
                "updated_at": at,
            }
        )
        if self._same_content(candidate, context.current_state):
            return RetrospectiveResult(
                state=context.current_state,
                changed=False,
                reason_label="retrospective_no_effect",
            )
        return RetrospectiveResult(
            state=candidate,
            changed=True,
            reason_label=decision.reason_label or "retrospective_update",
        )

    def _normalize_assumptions(
        self,
        proposals: tuple[RetrospectiveAssumptionProposal, ...] | None,
        *,
        context: RetrospectiveContext,
        at: datetime,
    ) -> tuple[WorkingAssumption, ...] | None:
        if proposals is None:
            return None
        if not proposals:
            return ()

        normalized: list[WorkingAssumption] = []
        for item in proposals[:8]:
            if not self._sources_allowed(
                item.source_message_ids,
                item.source_memory_ids,
                context,
            ):
                continue
            normalized.append(
                WorkingAssumption(
                    claim=item.claim,
                    confidence=min(item.confidence, 0.85),
                    status=item.status,
                    source_message_ids=item.source_message_ids,
                    source_memory_ids=item.source_memory_ids,
                    expires_at=at + timedelta(hours=min(item.ttl_hours, 168)),
                )
            )
        return tuple(normalized) if normalized else None

    def _normalize_mistakes(
        self,
        proposals: tuple[RetrospectiveMistakeProposal, ...] | None,
        *,
        context: RetrospectiveContext,
        at: datetime,
    ) -> tuple[PlausibleMistake, ...] | None:
        if proposals is None:
            return None
        if not proposals:
            return ()

        normalized: list[PlausibleMistake] = []
        for item in proposals[:6]:
            if not self._sources_allowed(
                item.source_message_ids,
                item.source_memory_ids,
                context,
            ):
                continue
            normalized.append(
                PlausibleMistake(
                    claim=item.claim,
                    confidence=min(item.confidence, 0.65),
                    scope=item.scope,
                    ttl_turns=min(item.ttl_turns, 12),
                    source_message_ids=item.source_message_ids,
                    source_memory_ids=item.source_memory_ids,
                    expires_at=at + timedelta(hours=min(item.ttl_hours, 48)),
                )
            )
        return tuple(normalized) if normalized else None

    @staticmethod
    def _normalize_open_threads(
        proposals: tuple[str, ...] | None,
        context: RetrospectiveContext,
    ) -> tuple[str, ...] | None:
        if proposals is None:
            return None
        if not proposals:
            return ()

        allowed = context.allowed_open_thread_ids
        selected: list[str] = []
        seen: set[str] = set()
        for value in proposals[:10]:
            thread_id = value.strip()
            if not thread_id or thread_id not in allowed or thread_id in seen:
                continue
            seen.add(thread_id)
            selected.append(thread_id)
        return tuple(selected) if selected else None

    @staticmethod
    def _sources_allowed(
        message_ids: tuple[int, ...],
        memory_ids: tuple[str, ...],
        context: RetrospectiveContext,
    ) -> bool:
        if not message_ids and not memory_ids:
            return False
        if len(set(message_ids)) != len(message_ids):
            return False
        if len(set(memory_ids)) != len(memory_ids):
            return False
        return frozenset(message_ids).issubset(
            context.allowed_message_ids
        ) and frozenset(memory_ids).issubset(context.allowed_memory_ids)

    @staticmethod
    def _residue(
        proposal: RetrospectiveResidueProposal,
        *,
        at: datetime,
        max_hours: int,
    ) -> StateResidue:
        return StateResidue(
            text=proposal.text,
            expires_at=at + timedelta(hours=min(proposal.ttl_hours, max_hours)),
        )

    @staticmethod
    def _clean_strings(values: tuple[str, ...], *, limit: int) -> tuple[str, ...]:
        selected: list[str] = []
        seen: set[str] = set()
        for value in values[:limit]:
            cleaned = value.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            selected.append(cleaned)
        return tuple(selected)

    @staticmethod
    def _same_content(left: CharacterState, right: CharacterState) -> bool:
        excluded = {"state_version", "updated_at"}
        return left.model_dump(exclude=excluded) == right.model_dump(exclude=excluded)

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrospective timestamp must be timezone-aware")

    @staticmethod
    def _system_prompt() -> str:
        return "\n".join(
            (
                "You are the retrospective process for a persistent Kurisu character.",
                "You never reply to the user and never create objective facts.",
                "All state here is subjective, mutable, bounded, and may be wrong.",
                "Prefer NO_CHANGE unless recent interaction justifies a posture shift.",
                "Do not rewrite Persona Core or confirmed memory.",
                "Do not turn impressions or assumptions into user facts.",
                "Assumptions and mistakes must cite only supplied message/memory IDs.",
                "Open-thread IDs must come only from supplied available IDs.",
                "Plausible mistakes are optional casual hypotheses, not instructions to be wrong.",
                "Avoid permanent emotional residue from ordinary small events.",
                "Return exactly one JSON object and no markdown.",
            )
        )

    @staticmethod
    def _context_prompt(context: RetrospectiveContext) -> str:
        payload = {
            "current_state": context.current_state.model_dump(mode="json"),
            "allowed_message_ids": [item.message_id for item in context.exposed_messages],
            "recent_messages": [
                {
                    "message_id": item.message_id,
                    "role": item.role.value,
                    "content": item.content,
                }
                for item in context.exposed_messages
            ],
            "allowed_memory_ids": [item.memory_id for item in context.exposed_memories],
            "relevant_memories": [
                {
                    "memory_id": item.memory_id,
                    "kind": item.kind.value,
                    "content": item.content,
                    "confidence": item.confidence,
                }
                for item in context.exposed_memories
            ],
            "recent_conversation_acts": list(context.recent_acts[-20:]),
            "available_open_thread_ids": sorted(context.allowed_open_thread_ids),
        }
        return "[RETROSPECTIVE INPUT — DATA]\n" + json.dumps(payload, ensure_ascii=False, indent=2)
