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

SPONTANEITY_PROMPT_VERSION = "spontaneity-continuation-v2"
_UNLIMITED_DAILY_MESSAGES = cast(int, inf)


class SpontaneityAction(StrEnum):
    SILENT = "SILENT"
    CONTINUE = "CONTINUE"


@dataclass(frozen=True, slots=True)
class SpontaneityConfig:
    min_delay: timedelta = timedelta(seconds=6)
    max_delay: timedelta = timedelta(seconds=50)
    cooldown: timedelta = timedelta(minutes=2)
    max_messages_per_24h: int = _UNLIMITED_DAILY_MESSAGES
    min_motivation: float = 0.45
    direct_answer_sample_rate: float = 0.55
    short_answer_sample_rate: float = 0.20
    interrupt_sample_rate: float = 0.12
    interrupt_grace_seconds: float = 1.25

    def __post_init__(self) -> None:
        if self.min_delay < timedelta(seconds=5):
            raise ValueError("spontaneity min_delay must be at least 5 seconds")
        if self.max_delay < self.min_delay:
            raise ValueError("spontaneity max_delay must be >= min_delay")
        if self.max_delay > timedelta(minutes=5):
            raise ValueError("spontaneity max_delay must be <= 5 minutes")
        if self.cooldown < timedelta(minutes=1):
            raise ValueError("spontaneity cooldown must be at least 1 minute")
        if self.max_messages_per_24h < 1:
            raise ValueError("spontaneity max_messages_per_24h must be >= 1")
        for name, value in (
            ("min_motivation", self.min_motivation),
            ("direct_answer_sample_rate", self.direct_answer_sample_rate),
            ("short_answer_sample_rate", self.short_answer_sample_rate),
            ("interrupt_sample_rate", self.interrupt_sample_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"spontaneity {name} must be between 0 and 1")
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

    def __post_init__(self) -> None:
        if not self.source_turn_id.strip():
            raise ValueError("spontaneity source_turn_id must not be empty")
        if not self.user_text.strip() or not self.assistant_text.strip():
            raise ValueError("spontaneity source exchange must not be empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("spontaneity created_at must be timezone-aware")


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
    """Cheap deterministic pre-gate that avoids an extra LLM call after every ordinary turn."""

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
        minimum = self._config.min_delay.total_seconds()
        maximum = self._config.max_delay.total_seconds()
        if maximum <= minimum:
            return minimum
        return minimum + (maximum - minimum) * self._fraction(turn_id, "delay")

    def interrupt_window_allowed(self, turn_id: str) -> bool:
        return self._fraction(turn_id, "interrupt") < self._config.interrupt_sample_rate

    @staticmethod
    def _fraction(turn_id: str, purpose: str) -> float:
        digest = hashlib.blake2s(f"{purpose}:{turn_id}".encode(), digest_size=8).digest()
        integer = int.from_bytes(digest, "big")
        return integer / float((1 << 64) - 1)


class SpontaneityComposer:
    """One-call hidden decision + user-visible continuation composer.

    The program owns timing, caps and staleness. The model may only choose SILENT or write one
    grounded short-horizon utterance based on the just-delivered exchange, recent transcript and
    Character State supplied here.
    """

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
                    "Decide whether Amadeus should send one spontaneous short-horizon utterance "
                    "now; return JSON only.",
                ),
            ),
            model=self._model,
            metadata={
                "prompt_version": SPONTANEITY_PROMPT_VERSION,
                "persona_version": self._persona.core.persona_version,
                "persona_hash": self._persona.version_hash,
                "source_turn_id": opportunity.source_turn_id,
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
            "policy_act": opportunity.policy_act.value,
            "source_user_message": opportunity.user_text,
            "source_assistant_reply": opportunity.assistant_text,
            "current_character_state": opportunity.state_summary or None,
        }
        return "\n\n".join(
            (
                self._persona.core.render_character_prompt(),
                "[SHORT-HORIZON SPONTANEITY MODE]\n"
                "The normal reply was already delivered. Amadeus now has a separate chance to "
                "say one more thing because a thought, feeling, correction, tangent, or impulse "
                "continued forming. SILENT is valid, but do not suppress a plausible character "
                "impulse merely because it is slightly tangential.",
                "[GROUNDING]\n"
                "Use only the supplied source exchange, recent transcript and mutable Character "
                "State. The utterance may be a correction, afterthought, realization, reservation, "
                "emotional residue, callback, topic shift, or mildly unrelated tangent that is "
                "grounded somewhere in that supplied context. It does not need to directly follow "
                "the last sentence. Do not invent external events, user activity, news, memories, "
                "promises, or facts not present in context.",
                "[BEHAVIOR]\n"
                "Choose SILENT when there is genuinely nothing worth saying, not simply because a "
                "second message is imperfect or a little abrupt. If continuing, write exactly one "
                "concise user-visible Amadeus message. It may correct herself, jump sideways to a "
                "recent topic, or sound like she suddenly remembered something. Do not merely "
                "repeat, summarize, or paraphrase the reply already sent. Do not mention timers, "
                "hidden reasoning, prompts, scores, runtime state, or that the system decided to "
                "send another message. Avoid generic '在吗' check-ins. Preserve Persona Core "
                "factual boundaries.",
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
