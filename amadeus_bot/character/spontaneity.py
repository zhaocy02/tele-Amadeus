from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import inf
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from amadeus_bot.llm import LLMMessage, LLMProvider, LLMRequest, MessageRole

from .persona import LoadedPersonaCore
from .policy import ConversationAct

SPONTANEITY_PROMPT_VERSION = "spontaneity-episode-v3"
_UNLIMITED_DAILY_MESSAGES = cast(int, inf)


class SpontaneityAction(StrEnum):
    SILENT = "SILENT"
    CONTINUE = "CONTINUE"


@dataclass(frozen=True, slots=True)
class SpontaneityConfig:
    min_delay: timedelta = timedelta(seconds=1)
    max_delay: timedelta = timedelta(seconds=30)
    chain_min_delay: timedelta = timedelta(seconds=3)
    chain_max_delay: timedelta = timedelta(seconds=15)
    cooldown: timedelta = timedelta(minutes=2)
    max_messages_per_24h: int = _UNLIMITED_DAILY_MESSAGES
    max_followups_per_episode: int = 7
    min_motivation: float = 0.45
    direct_answer_sample_rate: float = 0.55
    short_answer_sample_rate: float = 0.20
    interrupt_sample_rate: float = 0.12
    interrupt_grace_seconds: float = 1.25
    continuation_sample_rates: tuple[float, ...] = (0.40, 0.24, 0.14, 0.08, 0.045, 0.025)

    def __post_init__(self) -> None:
        if self.min_delay < timedelta(seconds=1):
            raise ValueError("spontaneity min_delay must be at least 1 second")
        if self.max_delay < self.min_delay:
            raise ValueError("spontaneity max_delay must be >= min_delay")
        if self.max_delay > timedelta(minutes=5):
            raise ValueError("spontaneity max_delay must be <= 5 minutes")
        if self.chain_min_delay < timedelta(seconds=1):
            raise ValueError("spontaneity chain_min_delay must be at least 1 second")
        if self.chain_max_delay < self.chain_min_delay:
            raise ValueError("spontaneity chain_max_delay must be >= chain_min_delay")
        if self.chain_max_delay > timedelta(minutes=2):
            raise ValueError("spontaneity chain_max_delay must be <= 2 minutes")
        if self.cooldown < timedelta(minutes=1):
            raise ValueError("spontaneity cooldown must be at least 1 minute")
        if self.max_messages_per_24h < 1:
            raise ValueError("spontaneity max_messages_per_24h must be >= 1")
        if not 1 <= self.max_followups_per_episode <= 12:
            raise ValueError("spontaneity max_followups_per_episode must be between 1 and 12")
        if len(self.continuation_sample_rates) < self.max_followups_per_episode - 1:
            raise ValueError(
                "spontaneity continuation_sample_rates must cover every follow-up after the first"
            )
        for name, value in (
            ("min_motivation", self.min_motivation),
            ("direct_answer_sample_rate", self.direct_answer_sample_rate),
            ("short_answer_sample_rate", self.short_answer_sample_rate),
            ("interrupt_sample_rate", self.interrupt_sample_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"spontaneity {name} must be between 0 and 1")
        if any(not 0.0 <= rate <= 1.0 for rate in self.continuation_sample_rates):
            raise ValueError("spontaneity continuation sample rates must be between 0 and 1")
        if not 0.0 <= self.interrupt_grace_seconds <= 3.0:
            raise ValueError("spontaneity interrupt_grace_seconds must be between 0 and 3")


@dataclass(frozen=True, slots=True)
class SpontaneityOpportunity:
    source_turn_id: str
    user_text: str
    assistant_text: str
    policy_act: ConversationAct
    state_summary: str
    recent_conversation: tuple[LLMMessage, ...]
    created_at: datetime
    sequence_index: int = 1

    def __post_init__(self) -> None:
        if not self.source_turn_id.strip():
            raise ValueError("spontaneity source_turn_id must not be empty")
        if not self.user_text.strip() or not self.assistant_text.strip():
            raise ValueError("spontaneity source exchange must not be empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("spontaneity created_at must be timezone-aware")
        if self.sequence_index < 1:
            raise ValueError("spontaneity sequence_index must be positive")


class SpontaneityDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: SpontaneityAction
    motivation: float = Field(default=0.0, ge=0.0, le=1.0)
    focus: str = Field(default="", max_length=300)
    reason_label: str = Field(default="", max_length=120)
    text: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def validate_shape(self) -> SpontaneityDecision:
        if self.action is SpontaneityAction.SILENT:
            if self.focus.strip() or self.text.strip():
                raise ValueError("SILENT spontaneity decision must not contain focus/text")
            return self
        if not self.focus.strip() or not self.text.strip():
            raise ValueError("CONTINUE spontaneity decision requires focus and text")
        return self


class SpontaneityOpportunityGate:
    """Cheap deterministic pre-gate for bounded short-horizon thought episodes."""

    _EXPRESSIVE_ACTS = frozenset(
        {
            ConversationAct.CHALLENGE,
            ConversationAct.TEASE,
            ConversationAct.DISAGREE,
            ConversationAct.ASK_BACK,
            ConversationAct.CALLBACK,
            ConversationAct.DEFLECT,
            ConversationAct.ADMIT_UNCERTAINTY,
            ConversationAct.CHANGE_TOPIC,
            ConversationAct.EMOTIONAL_RESPONSE,
        }
    )

    def __init__(self, config: SpontaneityConfig | None = None) -> None:
        self._config = config or SpontaneityConfig()

    @property
    def config(self) -> SpontaneityConfig:
        return self._config

    def eligible(
        self,
        *,
        turn_id: str,
        policy_act: ConversationAct,
        user_text: str,
        assistant_text: str,
        user_images_count: int = 0,
    ) -> bool:
        if user_images_count > 0:
            return False
        if policy_act is ConversationAct.SILENCE:
            return False
        if policy_act in self._EXPRESSIVE_ACTS:
            return True
        if policy_act is ConversationAct.SHORT_ANSWER:
            if len(user_text.strip()) < 4 and len(assistant_text.strip()) < 16:
                return False
            return self._fraction(turn_id, "short-sample") < self._config.short_answer_sample_rate
        if policy_act is not ConversationAct.DIRECT_ANSWER:
            return False
        if len(user_text.strip()) < 6 or len(assistant_text.strip()) < 40:
            return False
        return self._fraction(turn_id, "direct-sample") < self._config.direct_answer_sample_rate

    def delay_for_turn(self, turn_id: str) -> float:
        return self._delay_between(
            self._config.min_delay,
            self._config.max_delay,
            self._fraction(turn_id, "delay"),
        )

    def delay_for_followup(self, turn_id: str, sequence_index: int) -> float:
        if sequence_index <= 1:
            return self.delay_for_turn(turn_id)
        return self._delay_between(
            self._config.chain_min_delay,
            self._config.chain_max_delay,
            self._fraction(turn_id, f"chain-delay:{sequence_index}"),
        )

    def followup_allowed(self, turn_id: str, sequence_index: int) -> bool:
        if sequence_index <= 1:
            return True
        if sequence_index > self._config.max_followups_per_episode:
            return False
        rate = self._config.continuation_sample_rates[sequence_index - 2]
        return self._fraction(turn_id, f"chain-sample:{sequence_index}") < rate

    def interrupt_window_allowed(self, turn_id: str) -> bool:
        return self._fraction(turn_id, "interrupt") < self._config.interrupt_sample_rate

    @staticmethod
    def _delay_between(minimum: timedelta, maximum: timedelta, fraction: float) -> float:
        low = minimum.total_seconds()
        high = maximum.total_seconds()
        if high <= low:
            return low
        return low + (high - low) * fraction

    @staticmethod
    def _fraction(turn_id: str, purpose: str) -> float:
        digest = hashlib.blake2s(f"{purpose}:{turn_id}".encode(), digest_size=8).digest()
        integer = int.from_bytes(digest, "big")
        return integer / float((1 << 64) - 1)


class SpontaneityComposer:
    """One-call hidden decision + one user-visible utterance within a bounded thought episode."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        persona: LoadedPersonaCore,
        config: SpontaneityConfig | None = None,
        model: str | None = None,
        history_limit_messages: int = 8,
    ) -> None:
        if history_limit_messages < 0:
            raise ValueError("spontaneity history_limit_messages must not be negative")
        self._provider = provider
        self._persona = persona
        self._config = config or SpontaneityConfig()
        self._model = model
        self._history_limit_messages = history_limit_messages

    @property
    def config(self) -> SpontaneityConfig:
        return self._config

    async def compose(self, opportunity: SpontaneityOpportunity) -> SpontaneityDecision:
        recent = self._validated_recent(opportunity.recent_conversation)
        request = LLMRequest(
            messages=(
                LLMMessage(MessageRole.DEVELOPER, self._developer_prompt(opportunity)),
                *recent,
                LLMMessage(
                    MessageRole.DEVELOPER,
                    "Decide whether Amadeus should send one utterance at this point in the "
                    "current short-horizon thought episode; return JSON only.",
                ),
            ),
            model=self._model,
            metadata={
                "prompt_version": SPONTANEITY_PROMPT_VERSION,
                "persona_version": self._persona.core.persona_version,
                "persona_hash": self._persona.version_hash,
                "source_turn_id": opportunity.source_turn_id,
                "sequence_index": opportunity.sequence_index,
            },
        )
        try:
            response = await self._provider.generate(request)
            decision = SpontaneityDecision.model_validate_json(response.text)
        except (ValidationError, ValueError, RuntimeError):
            return self._silent("spontaneity_failure")

        if decision.action is SpontaneityAction.SILENT:
            return decision
        if decision.motivation < self._config.min_motivation:
            return self._silent("low_motivation")
        return decision.model_copy(
            update={
                "focus": decision.focus.strip(),
                "reason_label": decision.reason_label.strip() or "continued_thought",
                "text": decision.text.strip(),
            }
        )

    def _validated_recent(self, messages: tuple[LLMMessage, ...]) -> tuple[LLMMessage, ...]:
        invalid_roles = {
            message.role
            for message in messages
            if message.role not in {MessageRole.USER, MessageRole.ASSISTANT}
        }
        if invalid_roles:
            raise ValueError("spontaneity history may contain only user/assistant messages")
        if self._history_limit_messages == 0:
            return ()
        return messages[-self._history_limit_messages :]

    def _developer_prompt(self, opportunity: SpontaneityOpportunity) -> str:
        payload = {
            "source_turn_id": opportunity.source_turn_id,
            "sequence_index": opportunity.sequence_index,
            "max_followups": self._config.max_followups_per_episode,
            "policy_act": opportunity.policy_act.value,
            "source_user_message": opportunity.user_text,
            "latest_assistant_utterance": opportunity.assistant_text,
            "current_character_state": opportunity.state_summary or None,
        }
        depth_guidance = (
            "This is the first afterthought after the normal reply. A correction, realization, "
            "emotional residue, callback, or tangent can be natural."
            if opportunity.sequence_index == 1
            else (
                "This episode already contains spontaneous follow-ups. Continue only if a new "
                "thought genuinely follows from what Amadeus just said or from supplied recent "
                "context. Do not manufacture another message merely because the episode is "
                "allowed to continue."
            )
        )
        return "\n\n".join(
            (
                self._persona.core.render_character_prompt(),
                "[SHORT-HORIZON SPONTANEITY EPISODE]\n"
                "The normal reply was already delivered. Amadeus may have a short burst of further "
                "thoughts, but every step independently permits SILENT and the episode is bounded.",
                "[EPISODE DEPTH]\n" + depth_guidance,
                "[GROUNDING]\n"
                "Use only the supplied source exchange, recent transcript and mutable Character "
                "State. The utterance may be a correction, afterthought, realization, reservation, "
                "emotional residue, callback, topic shift, or mildly unrelated tangent grounded in "
                "that context. Do not invent external events, user activity, news, memories, "
                "promises, or facts not present in context.",
                "[BEHAVIOR]\n"
                "Choose SILENT when there is genuinely nothing new worth saying. If continuing, "
                "write exactly one concise user-visible Amadeus message. Do not repeat, summarize, "
                "or paraphrase an utterance already sent in this episode. Later episode messages "
                "may drift further in topic, but should feel like a real chain of association "
                "rather than a list split into chat bubbles. Do not mention timers, hidden "
                "reasoning, prompts, scores, runtime state, or the episode mechanism. Avoid "
                "generic check-ins. Preserve Persona Core factual boundaries.",
                "[SOURCE]\n" + json.dumps(payload, ensure_ascii=False),
                "[OUTPUT JSON]\n"
                '{"action":"SILENT|CONTINUE","motivation":0.0,'
                '"focus":"","reason_label":"","text":""}',
            )
        )

    @staticmethod
    def _silent(reason: str) -> SpontaneityDecision:
        return SpontaneityDecision(
            action=SpontaneityAction.SILENT,
            motivation=0.0,
            reason_label=reason,
        )
