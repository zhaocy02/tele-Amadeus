from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from amadeus_bot.character import (
    AutonomyAction,
    AutonomyDecision,
    AutonomyOpportunity,
    AutonomyOpportunityScheduler,
    AutonomyPlanner,
    AutonomySignal,
    AutonomySignalKind,
    CharacterState,
    SQLiteCharacterStateStore,
    StateResidue,
)
from amadeus_bot.memory import MemoryKind, MemoryRecord, StructuredMemoryRepository

from .autonomy_store import SQLiteAutonomyRuntimeStore, StoredAutonomyDelivery
from .session_store import ConversationSessionStore, StoredConversationMessage


@dataclass(frozen=True, slots=True)
class AutonomyRuntimeEvaluation:
    """One persisted autonomy opportunity evaluation; it is not a delivered message."""

    chat_id: int
    generation: int
    evaluated_at: datetime
    due: bool
    opportunity: AutonomyOpportunity | None
    decision: AutonomyDecision
    evaluation_id: int | None = None
    last_user_message_id: int | None = None

    @property
    def should_prepare_delivery(self) -> bool:
        return self.due and self.decision.action is not AutonomyAction.SILENT


@dataclass(frozen=True, slots=True)
class AutonomyRuntimePreview:
    """Read-only live autonomy state for human/operator status surfaces."""

    chat_id: int
    generation: int
    observed_at: datetime
    opportunity: AutonomyOpportunity
    guard_allowed: bool
    guard_reason_label: str
    idle_contact_drive: float
    contact_drive_motivation_bonus: float
    raw_motivation_threshold: float
    contact_urge: float
    strongest_signal: AutonomySignal | None
    last_opportunity_at: datetime | None
    next_opportunity_at: datetime
    opportunity_interval: timedelta
    min_user_idle: timedelta
    effective_proactive_cooldown: timedelta
    max_messages_per_24h: int
    max_consecutive_unanswered: int
    max_messages_per_sleep_session: int
    min_signal_salience: float
    base_model_motivation_threshold: float


class AutonomyRuntimeCoordinator:
    """Build grounded opportunities and persist cadence/delivery statistics without sending."""

    def __init__(
        self,
        *,
        planner: AutonomyPlanner,
        scheduler: AutonomyOpportunityScheduler,
        memory_repository: StructuredMemoryRepository,
        state_store: SQLiteCharacterStateStore,
        sessions: ConversationSessionStore,
        autonomy_store: SQLiteAutonomyRuntimeStore,
    ) -> None:
        self._planner = planner
        self._scheduler = scheduler
        self._memory_repository = memory_repository
        self._state_store = state_store
        self._sessions = sessions
        self._autonomy_store = autonomy_store

    async def preview(
        self,
        chat_id: int,
        *,
        at: datetime | None = None,
        do_not_disturb: bool = False,
        user_suppressed: bool = False,
        sleep_mode: bool = False,
        sleep_started_at: datetime | None = None,
    ) -> AutonomyRuntimePreview:
        """Build the exact current opportunity without model calls or persistence writes."""

        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        if sleep_started_at is not None:
            self._validate_aware(sleep_started_at)
        if sleep_mode and sleep_started_at is None:
            raise ValueError("sleep_mode autonomy preview requires sleep_started_at")

        generation = self._sessions.current_generation(chat_id)
        last_user_message_at = self._sessions.last_user_message_at(chat_id)
        stats = self._autonomy_store.delivery_stats(
            chat_id,
            generation,
            now=timestamp,
            last_user_message_at=last_user_message_at,
            sleep_started_at=sleep_started_at if sleep_mode else None,
        )
        state = await self._state_store.load_state()
        if state is None:
            state = CharacterState.initial(at=timestamp)
        effective_state = state.effective(at=timestamp)
        signals = await self._build_signals(chat_id, effective_state, at=timestamp)
        opportunity = AutonomyOpportunity(
            now=timestamp,
            signals=signals,
            last_user_message_at=last_user_message_at,
            last_autonomy_message_at=stats.last_autonomy_message_at,
            autonomy_messages_last_24h=stats.autonomy_messages_last_24h,
            consecutive_unanswered_autonomy=stats.consecutive_unanswered_autonomy,
            sleep_mode=sleep_mode,
            sleep_messages_since_start=stats.sleep_messages_since_start,
            do_not_disturb=do_not_disturb,
            user_suppressed=user_suppressed,
            current_state_summary=effective_state.render_prompt_context(at=timestamp),
            relationship_summary=effective_state.relationship_tone,
        )

        guard = self._planner._guard
        guard_result = guard.evaluate(opportunity)
        idle_contact_drive = guard.idle_contact_drive(opportunity)
        contact_drive_bonus = guard.contact_drive_motivation_bonus(opportunity)
        raw_threshold = max(0.0, guard.config.min_model_motivation - contact_drive_bonus)
        strongest_signal = max(signals, key=lambda signal: signal.salience, default=None)
        strongest_salience = 0.0 if strongest_signal is None else strongest_signal.salience
        contact_urge = (
            0.0
            if last_user_message_at is None
            else min(1.0, round(0.55 * idle_contact_drive + 0.45 * strongest_salience, 4))
        )

        last_opportunity_at = self._autonomy_store.last_opportunity_at(chat_id, generation)
        if last_opportunity_at is None:
            next_opportunity_at = timestamp
        else:
            candidate = self._scheduler.next_due(last_opportunity_at=last_opportunity_at)
            next_opportunity_at = timestamp if candidate <= timestamp else candidate
        opportunity_interval = self._scheduler.next_due(last_opportunity_at=timestamp) - timestamp

        return AutonomyRuntimePreview(
            chat_id=chat_id,
            generation=generation,
            observed_at=timestamp,
            opportunity=opportunity,
            guard_allowed=guard_result.allowed,
            guard_reason_label=guard_result.reason_label,
            idle_contact_drive=idle_contact_drive,
            contact_drive_motivation_bonus=contact_drive_bonus,
            raw_motivation_threshold=round(raw_threshold, 4),
            contact_urge=contact_urge,
            strongest_signal=strongest_signal,
            last_opportunity_at=last_opportunity_at,
            next_opportunity_at=next_opportunity_at,
            opportunity_interval=opportunity_interval,
            min_user_idle=guard.config.min_user_idle,
            effective_proactive_cooldown=guard.effective_proactive_cooldown(opportunity),
            max_messages_per_24h=guard.config.max_messages_per_24h,
            max_consecutive_unanswered=guard.config.max_consecutive_unanswered,
            max_messages_per_sleep_session=guard.config.max_messages_per_sleep_session,
            min_signal_salience=guard.config.min_signal_salience,
            base_model_motivation_threshold=guard.config.min_model_motivation,
        )

    async def evaluate(
        self,
        chat_id: int,
        *,
        at: datetime | None = None,
        do_not_disturb: bool = False,
        user_suppressed: bool = False,
        sleep_mode: bool = False,
        sleep_started_at: datetime | None = None,
    ) -> AutonomyRuntimeEvaluation:
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        if sleep_started_at is not None:
            self._validate_aware(sleep_started_at)
        if sleep_mode and sleep_started_at is None:
            raise ValueError("sleep_mode autonomy evaluation requires sleep_started_at")
        generation = self._sessions.current_generation(chat_id)
        last_opportunity = self._autonomy_store.last_opportunity_at(chat_id, generation)
        if not self._scheduler.is_due(now=timestamp, last_opportunity_at=last_opportunity):
            return AutonomyRuntimeEvaluation(
                chat_id=chat_id,
                generation=generation,
                evaluated_at=timestamp,
                due=False,
                opportunity=None,
                decision=self._silent("schedule_not_due"),
            )

        last_user_message_at = self._sessions.last_user_message_at(chat_id)
        last_user_message_id = self._sessions.last_user_message_id(chat_id)
        stats = self._autonomy_store.delivery_stats(
            chat_id,
            generation,
            now=timestamp,
            last_user_message_at=last_user_message_at,
            sleep_started_at=sleep_started_at if sleep_mode else None,
        )
        state = await self._state_store.load_state()
        if state is None:
            state = CharacterState.initial(at=timestamp)
        effective_state = state.effective(at=timestamp)
        signals = await self._build_signals(chat_id, effective_state, at=timestamp)
        opportunity = AutonomyOpportunity(
            now=timestamp,
            signals=signals,
            last_user_message_at=last_user_message_at,
            last_autonomy_message_at=stats.last_autonomy_message_at,
            autonomy_messages_last_24h=stats.autonomy_messages_last_24h,
            consecutive_unanswered_autonomy=stats.consecutive_unanswered_autonomy,
            sleep_mode=sleep_mode,
            sleep_messages_since_start=stats.sleep_messages_since_start,
            do_not_disturb=do_not_disturb,
            user_suppressed=user_suppressed,
            current_state_summary=effective_state.render_prompt_context(at=timestamp),
            relationship_summary=effective_state.relationship_tone,
        )

        # Persist cadence before any model call so provider failure/restart cannot create a retry loop.
        self._autonomy_store.mark_opportunity(chat_id, generation, at=timestamp)
        decision = await self._planner.decide(opportunity)
        stored = self._autonomy_store.record_evaluation(
            chat_id,
            generation,
            action=decision.action,
            reason_label=decision.reason_label,
            selected_signal_id=decision.selected_signal_id,
            motivation=decision.motivation,
            at=timestamp,
        )
        return AutonomyRuntimeEvaluation(
            chat_id=chat_id,
            generation=generation,
            evaluated_at=timestamp,
            due=True,
            opportunity=opportunity,
            decision=decision,
            evaluation_id=stored.evaluation_id,
            last_user_message_id=last_user_message_id,
        )

    def validate_delivery_candidate(self, evaluation: AutonomyRuntimeEvaluation) -> None:
        """Fail before external send when an autonomy evaluation is stale or non-deliverable."""

        self._validate_delivery_shape(evaluation)
        current_generation = self._sessions.current_generation(evaluation.chat_id)
        if current_generation != evaluation.generation:
            raise ValueError("conversation generation changed before autonomy delivery")

        opportunity = evaluation.opportunity
        assert opportunity is not None
        current_last_user_id = self._sessions.last_user_message_id(evaluation.chat_id)
        if (
            evaluation.last_user_message_id is not None
            and current_last_user_id != evaluation.last_user_message_id
        ):
            raise ValueError("user activity changed before autonomy delivery")
        current_last_user = self._sessions.last_user_message_at(evaluation.chat_id)
        if current_last_user != opportunity.last_user_message_at:
            raise ValueError("user activity timestamp changed before autonomy delivery")

        now = datetime.now(UTC)
        stats = self._autonomy_store.delivery_stats(
            evaluation.chat_id,
            evaluation.generation,
            now=now,
            last_user_message_at=current_last_user,
        )
        if stats.last_autonomy_message_at != opportunity.last_autonomy_message_at:
            raise ValueError("autonomy delivery state changed before external send")

    def record_confirmed_delivery(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        *,
        at: datetime | None = None,
    ) -> StoredAutonomyDelivery:
        """Strict record path for callers that have not crossed an external transport boundary."""

        self.validate_delivery_candidate(evaluation)
        return self._record_delivery(evaluation, at=at)

    def record_confirmed_external_delivery(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        *,
        at: datetime | None = None,
    ) -> StoredAutonomyDelivery:
        """Record a message after transport already confirmed delivery.

        The original evaluated generation is preserved even if it rotated during the external send.
        Callers must use ``validate_delivery_candidate`` immediately before sending.
        """

        self._validate_delivery_shape(evaluation)
        return self._record_delivery(evaluation, at=at)

    def _record_delivery(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        *,
        at: datetime | None,
    ) -> StoredAutonomyDelivery:
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        if timestamp < evaluation.evaluated_at:
            raise ValueError("autonomy delivery timestamp must not precede evaluation")
        selected_signal_id = evaluation.decision.selected_signal_id
        assert selected_signal_id is not None
        return self._autonomy_store.record_delivery(
            evaluation.chat_id,
            evaluation.generation,
            action=evaluation.decision.action,
            signal_id=selected_signal_id,
            at=timestamp,
            evaluation_id=evaluation.evaluation_id,
        )

    @staticmethod
    def _validate_delivery_shape(evaluation: AutonomyRuntimeEvaluation) -> None:
        if not evaluation.should_prepare_delivery:
            raise ValueError("only due non-silent autonomy evaluations may be delivered")
        if evaluation.opportunity is None:
            raise ValueError("deliverable autonomy evaluation requires opportunity context")
        selected_signal_id = evaluation.decision.selected_signal_id
        if selected_signal_id is None or not selected_signal_id.strip():
            raise ValueError("non-silent autonomy delivery requires selected signal")
        allowed_ids = {signal.signal_id for signal in evaluation.opportunity.signals}
        if selected_signal_id not in allowed_ids:
            raise ValueError("autonomy delivery selected signal is not in opportunity")

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
            limit=12,
            at=at,
        )
        signals: list[AutonomySignal] = [self._memory_signal(memory) for memory in memories]
        state_residues = (*state.current_preoccupations, *state.unresolved_feelings)
        for index, residue in enumerate(state_residues[:6]):
            signals.append(self._state_signal(state, residue, index=index, at=at))
        signals.extend(self._recent_conversation_signals(chat_id, at=at, limit=4))
        return tuple(signals[:24])

    @staticmethod
    def _memory_signal(memory: MemoryRecord) -> AutonomySignal:
        if memory.kind is MemoryKind.OPEN_THREAD:
            return AutonomySignal(
                signal_id="open-thread:" + memory.memory_id,
                kind=AutonomySignalKind.OPEN_THREAD,
                summary=memory.content,
                salience=memory.salience,
                source_thread_id=memory.memory_id,
            )
        kind = (
            AutonomySignalKind.RELATIONSHIP_MEMORY
            if memory.kind is MemoryKind.RELATIONSHIP
            else AutonomySignalKind.MEMORY
        )
        return AutonomySignal(
            signal_id="memory:" + memory.memory_id,
            kind=kind,
            summary=memory.content,
            salience=memory.salience,
            source_memory_id=memory.memory_id,
        )

    @classmethod
    def _state_signal(
        cls,
        state: CharacterState,
        residue: StateResidue,
        *,
        index: int,
        at: datetime,
    ) -> AutonomySignal:
        age_hours = max(0.0, (at - state.updated_at).total_seconds() / 3600)
        remaining_hours = max(0.0, (residue.expires_at - at).total_seconds() / 3600)
        freshness = max(0.0, 1.0 - min(age_hours, 24.0) / 24.0)
        persistence = min(1.0, remaining_hours / 24.0)
        salience = cls._clamp(0.40 + 0.25 * freshness + 0.25 * persistence - 0.03 * index)
        return AutonomySignal(
            signal_id=f"state-preoccupation:{state.state_version}:{index}",
            kind=AutonomySignalKind.STATE_PREOCCUPATION,
            summary=residue.text,
            salience=salience,
        )

    def _recent_conversation_signals(
        self,
        chat_id: int,
        *,
        at: datetime,
        limit: int,
    ) -> tuple[AutonomySignal, ...]:
        records = self._sessions.history_records(chat_id, 16)
        candidates = [record for record in reversed(records) if record.role.value == "user"]
        signals: list[AutonomySignal] = []
        for record in candidates:
            if len(signals) >= limit:
                break
            signal = self._conversation_signal(record, at=at)
            if signal is not None:
                signals.append(signal)
        return tuple(signals)

    @classmethod
    def _conversation_signal(
        cls,
        record: StoredConversationMessage,
        *,
        at: datetime,
    ) -> AutonomySignal | None:
        content = " ".join(record.content.split())
        if not content or content.startswith("/") or content == "[User sent a photo]":
            return None
        if len(content) < 6:
            return None
        age_hours = max(0.0, (at - record.created_at).total_seconds() / 3600)
        recency = max(0.0, 1.0 - min(age_hours, 24.0) / 24.0)
        richness = min(1.0, len(content) / 180.0)
        open_markers = ("?", "？", "之后", "以后", "下次", "还想", "再聊", "觉得", "为什么", "怎么")
        openness = 1.0 if any(marker in content for marker in open_markers) else 0.0
        salience = cls._clamp(0.32 + 0.28 * recency + 0.16 * richness + 0.14 * openness)
        summary = content if len(content) <= 420 else content[:419].rstrip() + "…"
        source = f"conversation-message:{record.message_id}"
        return AutonomySignal(
            signal_id="recent-conversation:" + str(record.message_id),
            kind=AutonomySignalKind.RECENT_CONVERSATION,
            summary=summary,
            salience=salience,
            source_thread_id=source,
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, round(value, 4)))

    @staticmethod
    def _silent(reason: str) -> AutonomyDecision:
        return AutonomyDecision(
            action=AutonomyAction.SILENT,
            motivation=0.0,
            reason_label=reason,
        )

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("autonomy runtime timestamp must be timezone-aware")
