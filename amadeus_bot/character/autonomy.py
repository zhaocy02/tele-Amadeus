from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from amadeus_bot.llm import LLMMessage, LLMProvider, LLMRequest, MessageRole

AUTONOMY_PROMPT_VERSION = "autonomy-policy-v3-stronger-idle-drive"

_IDLE_CONTACT_DRIVE_POINTS: tuple[tuple[float, float], ...] = (
    (0.05, 0.05),
    (1.0 / 12.0, 0.12),
    (0.25, 0.30),
    (0.5, 0.45),
    (1.0, 0.60),
    (2.0, 0.75),
    (4.0, 0.88),
    (8.0, 0.95),
    (24.0, 1.00),
)


class AutonomyAction(StrEnum):
    SILENT = "SILENT"
    FOLLOW_UP = "FOLLOW_UP"
    CALLBACK = "CALLBACK"
    ASK = "ASK"
    TEASE = "TEASE"
    SHARE_THOUGHT = "SHARE_THOUGHT"


class AutonomySignalKind(StrEnum):
    OPEN_THREAD = "open_thread"
    MEMORY = "memory"
    RELATIONSHIP_MEMORY = "relationship_memory"
    STATE_PREOCCUPATION = "state_preoccupation"
    RECENT_CONVERSATION = "recent_conversation"


class AutonomySignal(BaseModel):
    """Program-supplied reason that may justify a proactive character action."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_id: str = Field(min_length=1, max_length=120)
    kind: AutonomySignalKind
    summary: str = Field(min_length=1, max_length=500)
    salience: float = Field(ge=0.0, le=1.0)
    source_memory_id: str | None = Field(default=None, max_length=160)
    source_thread_id: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_signal(self) -> AutonomySignal:
        if not self.signal_id.strip() or not self.summary.strip():
            raise ValueError("autonomy signal id/summary must not be blank")
        if (
            self.kind in {AutonomySignalKind.OPEN_THREAD, AutonomySignalKind.RECENT_CONVERSATION}
            and (self.source_thread_id is None or not self.source_thread_id.strip())
        ):
            raise ValueError("thread-backed autonomy signal requires source_thread_id")
        if (
            self.kind
            in {
                AutonomySignalKind.MEMORY,
                AutonomySignalKind.RELATIONSHIP_MEMORY,
            }
            and (self.source_memory_id is None or not self.source_memory_id.strip())
        ):
            raise ValueError("memory-backed autonomy signal requires source_memory_id")
        return self


@dataclass(frozen=True, slots=True)
class AutonomyOpportunity:
    """One program-owned opportunity to consider a proactive character action."""

    now: datetime
    signals: tuple[AutonomySignal, ...] = ()
    last_user_message_at: datetime | None = None
    last_autonomy_message_at: datetime | None = None
    autonomy_messages_last_24h: int = 0
    consecutive_unanswered_autonomy: int = 0
    sleep_mode: bool = False
    sleep_messages_since_start: int = 0
    do_not_disturb: bool = False
    user_suppressed: bool = False
    current_state_summary: str = ""
    relationship_summary: str = ""

    def __post_init__(self) -> None:
        _validate_aware("autonomy opportunity now", self.now)
        for name, value in (
            ("last_user_message_at", self.last_user_message_at),
            ("last_autonomy_message_at", self.last_autonomy_message_at),
        ):
            if value is None:
                continue
            _validate_aware(name, value)
            if value > self.now:
                raise ValueError(f"{name} must not be in the future")
        if self.autonomy_messages_last_24h < 0:
            raise ValueError("autonomy_messages_last_24h must not be negative")
        if self.consecutive_unanswered_autonomy < 0:
            raise ValueError("consecutive_unanswered_autonomy must not be negative")
        if self.sleep_messages_since_start < 0:
            raise ValueError("sleep_messages_since_start must not be negative")
        signal_ids = tuple(signal.signal_id.strip() for signal in self.signals)
        if len(set(signal_ids)) != len(signal_ids):
            raise ValueError("autonomy signal IDs must be unique")


@dataclass(frozen=True, slots=True)
class AutonomyScheduleConfig:
    """Cadence for producing opportunities, never a cadence for sending messages."""

    check_interval: timedelta = timedelta(hours=1)

    def __post_init__(self) -> None:
        if self.check_interval < timedelta(minutes=1):
            raise ValueError("autonomy check_interval must be at least 1 minute")


class AutonomyOpportunityScheduler:
    """Pure schedule helper that says when another opportunity may be evaluated."""

    def __init__(self, config: AutonomyScheduleConfig | None = None) -> None:
        self._config = config or AutonomyScheduleConfig()

    def is_due(self, *, now: datetime, last_opportunity_at: datetime | None) -> bool:
        _validate_aware("autonomy schedule now", now)
        if last_opportunity_at is None:
            return True
        _validate_aware("last_opportunity_at", last_opportunity_at)
        if last_opportunity_at > now:
            raise ValueError("last_opportunity_at must not be in the future")
        return now - last_opportunity_at >= self._config.check_interval

    def next_due(self, *, last_opportunity_at: datetime) -> datetime:
        _validate_aware("last_opportunity_at", last_opportunity_at)
        return last_opportunity_at + self._config.check_interval


@dataclass(frozen=True, slots=True)
class AutonomyGuardConfig:
    min_user_idle: timedelta = timedelta(hours=2)
    proactive_cooldown: timedelta = timedelta(hours=6)
    max_messages_per_24h: int = 2
    max_consecutive_unanswered: int = 2
    max_messages_per_sleep_session: int = 2
    min_signal_salience: float = 0.6
    min_model_motivation: float = 0.55
    idle_drive_max_motivation_bonus: float = 0.12

    def __post_init__(self) -> None:
        if self.min_user_idle < timedelta(minutes=1):
            raise ValueError("min_user_idle must be at least 1 minute")
        if self.proactive_cooldown < timedelta(minutes=30):
            raise ValueError("proactive_cooldown must be at least 30 minutes")
        if self.max_messages_per_24h < 1:
            raise ValueError("max_messages_per_24h must be >= 1")
        if self.max_consecutive_unanswered < 1:
            raise ValueError("max_consecutive_unanswered must be >= 1")
        if self.max_messages_per_sleep_session < 1:
            raise ValueError("max_messages_per_sleep_session must be >= 1")
        if not 0.0 <= self.min_signal_salience <= 1.0:
            raise ValueError("min_signal_salience must be between 0 and 1")
        if not 0.0 <= self.min_model_motivation <= 1.0:
            raise ValueError("min_model_motivation must be between 0 and 1")
        if not 0.0 <= self.idle_drive_max_motivation_bonus <= 1.0:
            raise ValueError("idle_drive_max_motivation_bonus must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class AutonomyGuardResult:
    allowed: bool
    reason_label: str
    eligible_signals: tuple[AutonomySignal, ...] = ()


class AutonomyGuard:
    """Program-owned hard gate evaluated before any autonomy LLM call."""

    def __init__(self, config: AutonomyGuardConfig | None = None) -> None:
        self._config = config or AutonomyGuardConfig()

    @property
    def config(self) -> AutonomyGuardConfig:
        return self._config

    def effective_proactive_cooldown(self, opportunity: AutonomyOpportunity) -> timedelta:
        """Back off after ignored proactive messages while keeping a 30m+ base cooldown."""

        exponent = min(
            opportunity.consecutive_unanswered_autonomy,
            max(0, self._config.max_consecutive_unanswered - 1),
        )
        multiplier: int = 1 << exponent
        return self._config.proactive_cooldown * multiplier

    def idle_contact_drive(self, opportunity: AutonomyOpportunity) -> float:
        """Return a bounded willingness-to-reach-out factor derived only from user idle time."""

        last_user = opportunity.last_user_message_at
        if last_user is None:
            return 0.0
        idle_hours = max(0.0, (opportunity.now - last_user).total_seconds() / 3600.0)
        first_hours, first_drive = _IDLE_CONTACT_DRIVE_POINTS[0]
        if idle_hours <= first_hours:
            return first_drive
        for index in range(1, len(_IDLE_CONTACT_DRIVE_POINTS)):
            right_hours, right_drive = _IDLE_CONTACT_DRIVE_POINTS[index]
            left_hours, left_drive = _IDLE_CONTACT_DRIVE_POINTS[index - 1]
            if idle_hours <= right_hours:
                fraction = (idle_hours - left_hours) / (right_hours - left_hours)
                return round(left_drive + (right_drive - left_drive) * fraction, 4)
        return _IDLE_CONTACT_DRIVE_POINTS[-1][1]

    def contact_drive_motivation_bonus(self, opportunity: AutonomyOpportunity) -> float:
        """Convert idle contact drive into a small soft bonus, never a standalone reason."""

        return round(
            self.idle_contact_drive(opportunity) * self._config.idle_drive_max_motivation_bonus,
            4,
        )

    def effective_model_motivation(
        self,
        motivation: float,
        opportunity: AutonomyOpportunity,
    ) -> float:
        if not 0.0 <= motivation <= 1.0:
            raise ValueError("model motivation must be between 0 and 1")
        return min(1.0, round(motivation + self.contact_drive_motivation_bonus(opportunity), 4))

    def evaluate(self, opportunity: AutonomyOpportunity) -> AutonomyGuardResult:
        if opportunity.user_suppressed:
            return self._blocked("user_suppressed")
        if opportunity.do_not_disturb:
            return self._blocked("do_not_disturb")
        if opportunity.last_user_message_at is None:
            return self._blocked("no_user_history")
        if opportunity.autonomy_messages_last_24h >= self._config.max_messages_per_24h:
            return self._blocked("daily_cap")
        if (
            opportunity.consecutive_unanswered_autonomy
            >= self._config.max_consecutive_unanswered
        ):
            return self._blocked("unanswered_cap")
        if (
            opportunity.sleep_mode
            and opportunity.sleep_messages_since_start
            >= self._config.max_messages_per_sleep_session
        ):
            return self._blocked("sleep_session_cap")
        if opportunity.now - opportunity.last_user_message_at < self._config.min_user_idle:
            return self._blocked("user_recently_active")
        if opportunity.last_autonomy_message_at is not None:
            cooldown = self.effective_proactive_cooldown(opportunity)
            if opportunity.now - opportunity.last_autonomy_message_at < cooldown:
                return self._blocked("proactive_cooldown")

        eligible = tuple(
            signal
            for signal in opportunity.signals
            if signal.salience >= self._config.min_signal_salience
        )
        if not eligible:
            return self._blocked("no_salient_signal")
        return AutonomyGuardResult(
            allowed=True,
            reason_label="guard_pass",
            eligible_signals=eligible,
        )

    @staticmethod
    def _blocked(reason: str) -> AutonomyGuardResult:
        return AutonomyGuardResult(allowed=False, reason_label=reason)


class AutonomyDecision(BaseModel):
    """Validated hidden autonomy decision. It is not the user-visible message."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: AutonomyAction
    selected_signal_id: str | None = Field(default=None, max_length=120)
    motivation: float = Field(default=0.0, ge=0.0, le=1.0)
    focus: str = Field(default="", max_length=300)
    reason_label: str = ""

    @model_validator(mode="after")
    def validate_shape(self) -> AutonomyDecision:
        if self.action is AutonomyAction.SILENT:
            if self.selected_signal_id is not None or self.focus.strip():
                raise ValueError("SILENT must not contain signal/focus")
            return self
        if self.selected_signal_id is None or not self.selected_signal_id.strip():
            raise ValueError("non-silent autonomy action requires selected_signal_id")
        if not self.focus.strip():
            raise ValueError("non-silent autonomy action requires focus")
        return self


class AutonomyPlanner:
    """Apply hard guards, then ask an LLM whether a grounded proactive action is justified."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        guard: AutonomyGuard | None = None,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._guard = guard or AutonomyGuard()
        self._model = model

    async def decide(self, opportunity: AutonomyOpportunity) -> AutonomyDecision:
        guard_result = self._guard.evaluate(opportunity)
        if not guard_result.allowed:
            return self._silent("guard_" + guard_result.reason_label)

        idle_contact_drive = self._guard.idle_contact_drive(opportunity)
        contact_drive_bonus = self._guard.contact_drive_motivation_bonus(opportunity)
        request = LLMRequest(
            messages=(
                LLMMessage(MessageRole.SYSTEM, self._system_prompt()),
                LLMMessage(
                    MessageRole.USER,
                    self._context_prompt(
                        opportunity,
                        guard_result.eligible_signals,
                        idle_contact_drive=idle_contact_drive,
                        contact_drive_motivation_bonus=contact_drive_bonus,
                    ),
                ),
            ),
            model=self._model,
            metadata={"prompt_version": AUTONOMY_PROMPT_VERSION},
        )
        try:
            response = await self._provider.generate(request)
            decision = AutonomyDecision.model_validate_json(response.text)
        except (ValidationError, ValueError, RuntimeError):
            return self._silent("autonomy_failure")
        return self._normalize(decision, opportunity, guard_result.eligible_signals)

    def _normalize(
        self,
        decision: AutonomyDecision,
        opportunity: AutonomyOpportunity,
        eligible_signals: tuple[AutonomySignal, ...],
    ) -> AutonomyDecision:
        if decision.action is AutonomyAction.SILENT:
            return decision
        effective_motivation = self._guard.effective_model_motivation(
            decision.motivation,
            opportunity,
        )
        if effective_motivation < self._guard.config.min_model_motivation:
            return self._silent("low_motivation")

        by_id = {signal.signal_id: signal for signal in eligible_signals}
        selected_id = decision.selected_signal_id
        if selected_id is None or selected_id not in by_id:
            return self._silent("unauthorized_signal")
        selected = by_id[selected_id]
        if not self._action_matches_signal(decision.action, selected.kind):
            return self._silent("action_signal_mismatch")
        if (
            decision.action is AutonomyAction.TEASE
            and opportunity.consecutive_unanswered_autonomy > 0
        ):
            return self._silent("tease_after_unanswered")
        return decision.model_copy(
            update={
                "selected_signal_id": selected_id.strip(),
                "focus": decision.focus.strip(),
                "motivation": effective_motivation,
            }
        )

    @staticmethod
    def _action_matches_signal(action: AutonomyAction, kind: AutonomySignalKind) -> bool:
        if action is AutonomyAction.FOLLOW_UP:
            return kind in {
                AutonomySignalKind.OPEN_THREAD,
                AutonomySignalKind.RECENT_CONVERSATION,
            }
        if action is AutonomyAction.CALLBACK:
            return kind in {
                AutonomySignalKind.MEMORY,
                AutonomySignalKind.RELATIONSHIP_MEMORY,
            }
        if action is AutonomyAction.TEASE:
            return kind in {
                AutonomySignalKind.MEMORY,
                AutonomySignalKind.RELATIONSHIP_MEMORY,
            }
        return action in {AutonomyAction.ASK, AutonomyAction.SHARE_THOUGHT}

    @staticmethod
    def _silent(reason: str) -> AutonomyDecision:
        return AutonomyDecision(
            action=AutonomyAction.SILENT,
            motivation=0.0,
            reason_label=reason,
        )

    @staticmethod
    def _system_prompt() -> str:
        actions = "|".join(action.value for action in AutonomyAction)
        return "\n".join(
            (
                "You are the autonomy policy for a persistent Kurisu character.",
                "Do not write the user-visible message. Return one JSON object only.",
                "SILENT is always valid and should remain a normal outcome.",
                "A proactive action requires a concrete supplied signal, never a fabricated event.",
                "Do not invent news, reminders, user activity, or real-world developments.",
                (
                    "Do not choose generic check-ins such as 'are you there?' "
                    "without a supplied reason."
                ),
                (
                    "idle_contact_drive is a soft willingness-to-reach-out factor: as it rises, "
                    "be progressively more willing to act on a valid supplied signal."
                ),
                (
                    "idle_contact_drive is never a signal or standalone reason; "
                    "do not turn elapsed time into a generic check-in or claim "
                    "the user has been absent."
                ),
                (
                    "Set motivation from the grounded impulse itself; the program applies the "
                    "listed contact-drive motivation bonus separately."
                ),
                "Prefer not to interrupt unless the shared context genuinely supports it.",
                "Use only one eligible signal_id supplied in the input.",
                "Schema:",
                "{",
                f'  "action": "{actions}",',
                '  "selected_signal_id": null,',
                '  "motivation": 0.0,',
                '  "focus": "",',
                '  "reason_label": ""',
                "}",
            )
        )

    @staticmethod
    def _context_prompt(
        opportunity: AutonomyOpportunity,
        eligible_signals: tuple[AutonomySignal, ...],
        *,
        idle_contact_drive: float,
        contact_drive_motivation_bonus: float,
    ) -> str:
        assert opportunity.last_user_message_at is not None
        payload = {
            "hours_since_last_user_message": round(
                (opportunity.now - opportunity.last_user_message_at).total_seconds() / 3600,
                2,
            ),
            "idle_contact_drive": idle_contact_drive,
            "contact_drive_motivation_bonus": contact_drive_motivation_bonus,
            "hours_since_last_autonomy_message": (
                None
                if opportunity.last_autonomy_message_at is None
                else round(
                    (
                        opportunity.now - opportunity.last_autonomy_message_at
                    ).total_seconds()
                    / 3600,
                    2,
                )
            ),
            "autonomy_messages_last_24h": opportunity.autonomy_messages_last_24h,
            "consecutive_unanswered_autonomy": opportunity.consecutive_unanswered_autonomy,
            "sleep_mode": opportunity.sleep_mode,
            "sleep_messages_since_start": opportunity.sleep_messages_since_start,
            "current_character_state": opportunity.current_state_summary or None,
            "relationship_context": opportunity.relationship_summary or None,
            "eligible_signals": [
                {
                    "signal_id": signal.signal_id,
                    "kind": signal.kind.value,
                    "summary": signal.summary,
                    "salience": signal.salience,
                }
                for signal in eligible_signals
            ],
        }
        return "[AUTONOMY INPUT — DATA]\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
