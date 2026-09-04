from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


@dataclass(frozen=True, slots=True)
class CharacterStateSnapshot:
    """Versioned mutable-character-state snapshot, distinct from objective runtime facts."""

    version: int
    updated_at: datetime
    values: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("character state version must be >= 1")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


class AssumptionStatus(StrEnum):
    ACTIVE = "active"
    WEAKENED = "weakened"
    REJECTED = "rejected"


class MistakeScope(StrEnum):
    CASUAL_ONLY = "casual_only"
    GENERAL = "general"


class StateResidue(BaseModel):
    """Short-lived subjective posture with deterministic time expiry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=300)
    expires_at: datetime

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("state residue text must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_residue(self) -> StateResidue:
        _validate_aware("state residue expires_at", self.expires_at)
        return self

    def is_active(self, at: datetime) -> bool:
        _validate_aware("at", at)
        return self.expires_at > at


class WorkingAssumption(BaseModel):
    """Subjective hypothesis that may be weakened or rejected later."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=400)
    confidence: float = Field(ge=0.0, le=0.85)
    status: AssumptionStatus = AssumptionStatus.ACTIVE
    source_message_ids: tuple[int, ...] = ()
    source_memory_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None

    @field_validator("claim")
    @classmethod
    def normalize_claim(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("working assumption claim must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_assumption(self) -> WorkingAssumption:
        _validate_sources(self.source_message_ids, self.source_memory_ids)
        if self.expires_at is not None:
            _validate_aware("working assumption expires_at", self.expires_at)
        return self

    def is_active(self, at: datetime) -> bool:
        _validate_aware("at", at)
        if self.status is not AssumptionStatus.ACTIVE:
            return False
        return self.expires_at is None or self.expires_at > at


class PlausibleMistake(BaseModel):
    """Bounded character hypothesis that may color casual interpretation but is not a fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(min_length=1, max_length=400)
    confidence: float = Field(ge=0.0, le=0.65)
    scope: MistakeScope = MistakeScope.CASUAL_ONLY
    ttl_turns: int = Field(ge=1, le=12)
    source_message_ids: tuple[int, ...] = ()
    source_memory_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None

    @field_validator("claim")
    @classmethod
    def normalize_claim(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("plausible mistake claim must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_mistake(self) -> PlausibleMistake:
        _validate_sources(self.source_message_ids, self.source_memory_ids)
        if not self.source_message_ids and not self.source_memory_ids:
            raise ValueError("plausible mistake requires provenance")
        if self.expires_at is not None:
            _validate_aware("plausible mistake expires_at", self.expires_at)
        return self

    def is_active(self, at: datetime) -> bool:
        _validate_aware("at", at)
        return self.expires_at is None or self.expires_at > at


class CharacterState(BaseModel):
    """Typed subjective Character State, explicitly separate from objective facts/memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    state_version: int = Field(ge=1)
    updated_at: datetime
    emotional_stance: StateResidue | None = None
    relationship_tone: str = Field(default="baseline", max_length=200)
    current_preoccupations: tuple[StateResidue, ...] = Field(default=(), max_length=8)
    working_assumptions: tuple[WorkingAssumption, ...] = Field(default=(), max_length=8)
    unresolved_feelings: tuple[StateResidue, ...] = Field(default=(), max_length=6)
    plausible_mistakes: tuple[PlausibleMistake, ...] = Field(default=(), max_length=6)
    next_reaction_bias: StateResidue | None = None
    avoid_sounding_like: tuple[str, ...] = Field(default=(), max_length=8)
    open_thread_ids: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("relationship_tone")
    @classmethod
    def normalize_relationship_tone(cls, value: str) -> str:
        return value.strip() or "baseline"

    @field_validator("avoid_sounding_like", "open_thread_ids")
    @classmethod
    def normalize_string_tuple(cls, value: tuple[str, ...], info: Any) -> tuple[str, ...]:
        return _clean_unique_strings(info.field_name, value)

    @model_validator(mode="after")
    def validate_state(self) -> CharacterState:
        _validate_aware("character state updated_at", self.updated_at)
        return self

    @classmethod
    def initial(cls, *, at: datetime) -> CharacterState:
        _validate_aware("character state initial timestamp", at)
        return cls(state_version=1, updated_at=at)

    @classmethod
    def from_snapshot(cls, snapshot: CharacterStateSnapshot) -> CharacterState:
        payload = dict(snapshot.values)
        payload.update(
            {
                "state_version": snapshot.version,
                "updated_at": snapshot.updated_at,
            }
        )
        return cls.model_validate(payload)

    def to_snapshot(self) -> CharacterStateSnapshot:
        values = self.model_dump(
            mode="json",
            exclude={"state_version", "updated_at"},
        )
        return CharacterStateSnapshot(
            version=self.state_version,
            updated_at=self.updated_at,
            values=values,
        )

    def effective(self, *, at: datetime) -> CharacterState:
        """Return the non-mutating state view whose transient residues are still active."""

        _validate_aware("character state effective timestamp", at)
        emotional = (
            self.emotional_stance
            if self.emotional_stance is not None and self.emotional_stance.is_active(at)
            else None
        )
        reaction = (
            self.next_reaction_bias
            if self.next_reaction_bias is not None and self.next_reaction_bias.is_active(at)
            else None
        )
        return self.model_copy(
            update={
                "emotional_stance": emotional,
                "current_preoccupations": tuple(
                    item for item in self.current_preoccupations if item.is_active(at)
                ),
                "working_assumptions": tuple(
                    item for item in self.working_assumptions if item.is_active(at)
                ),
                "unresolved_feelings": tuple(
                    item for item in self.unresolved_feelings if item.is_active(at)
                ),
                "plausible_mistakes": tuple(
                    item for item in self.plausible_mistakes if item.is_active(at)
                ),
                "next_reaction_bias": reaction,
            }
        )

    def advance_turn(self, *, at: datetime) -> CharacterState:
        """Persist deterministic turn aging without asking an LLM to manage TTL counters."""

        current = self.effective(at=at)
        mistakes = tuple(
            item.model_copy(update={"ttl_turns": item.ttl_turns - 1})
            for item in current.plausible_mistakes
            if item.ttl_turns > 1
        )
        if mistakes == self.plausible_mistakes and current == self:
            return self
        return current.model_copy(
            update={
                "state_version": self.state_version + 1,
                "updated_at": at,
                "plausible_mistakes": mistakes,
            }
        )

    def render_prompt_context(self, *, at: datetime) -> str:
        state = self.effective(at=at)
        sections = [
            "[CHARACTER STATE — SUBJECTIVE, MUTABLE, MAY BE WRONG]",
            f"relationship_tone: {state.relationship_tone}",
        ]
        if state.emotional_stance is not None:
            sections.append(f"emotional_stance: {state.emotional_stance.text}")
        if state.current_preoccupations:
            sections.append(
                "current_preoccupations:\n"
                + "\n".join(f"- {item.text}" for item in state.current_preoccupations)
            )
        if state.working_assumptions:
            sections.append(
                "working_assumptions (not facts):\n"
                + "\n".join(
                    f"- {item.claim} (confidence={item.confidence:.2f})"
                    for item in state.working_assumptions
                )
            )
        if state.unresolved_feelings:
            sections.append(
                "unresolved_feelings:\n"
                + "\n".join(f"- {item.text}" for item in state.unresolved_feelings)
            )
        if state.plausible_mistakes:
            sections.append(
                "plausible_mistakes (casual hypotheses, never factual authority):\n"
                + "\n".join(
                    f"- {item.claim} (confidence={item.confidence:.2f}, ttl={item.ttl_turns})"
                    for item in state.plausible_mistakes
                )
            )
        if state.next_reaction_bias is not None:
            sections.append(f"next_reaction_bias: {state.next_reaction_bias.text}")
        if state.avoid_sounding_like:
            sections.append(
                "avoid_sounding_like:\n"
                + "\n".join(f"- {item}" for item in state.avoid_sounding_like)
            )
        if state.open_thread_ids:
            sections.append("open_thread_ids: " + ", ".join(state.open_thread_ids))
        return "\n\n".join(sections)


def _validate_sources(message_ids: tuple[int, ...], memory_ids: tuple[str, ...]) -> None:
    if any(message_id <= 0 for message_id in message_ids):
        raise ValueError("state source_message_ids must contain positive IDs")
    if len(set(message_ids)) != len(message_ids):
        raise ValueError("state source_message_ids must not contain duplicates")
    cleaned_memory_ids = tuple(memory_id.strip() for memory_id in memory_ids)
    if any(not memory_id for memory_id in cleaned_memory_ids):
        raise ValueError("state source_memory_ids must not contain blank IDs")
    if len(set(cleaned_memory_ids)) != len(cleaned_memory_ids):
        raise ValueError("state source_memory_ids must not contain duplicates")


def _clean_unique_strings(name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(value.strip() for value in values)
    if any(not value for value in cleaned):
        raise ValueError(f"{name} must not contain blank values")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{name} must not contain duplicates")
    return cleaned


def _validate_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
