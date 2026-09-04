from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from amadeus_bot.character import AutonomyMessageGenerator
from amadeus_bot.llm import LLMImage
from amadeus_bot.runtime import (
    AutonomyRuntimeCoordinator,
    AutonomyRuntimeEvaluation,
    ConversationSessionStore,
    PreparedV2Turn,
    SQLiteRuntimePreferenceStore,
    SQLiteSpontaneityStore,
    SQLiteTurnTelemetryStore,
    StoredAutonomyConversationMessage,
    StoredAutonomyDelivery,
    StoredSpontaneityConversationMessage,
    StoredSpontaneityDelivery,
    TurnTelemetryRecord,
    V2ConversationCoordinator,
    V2FinalizeResult,
)

from .adapter import IncomingMessage, TelegramGateway

_MAX_PHOTO_BYTES = 8 * 1024 * 1024
_PHOTO_MARKER = "[User sent a photo]"


@dataclass(frozen=True, slots=True)
class V2TelegramUserDeliveryResult:
    prepared: PreparedV2Turn
    telegram_message_id: int
    finalize: V2FinalizeResult | None
    time_to_send_ms: int = 0
    finalize_ms: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class V2TelegramAutonomyDeliveryResult:
    evaluation: AutonomyRuntimeEvaluation
    text: str
    telegram_message_id: int
    stored_delivery: StoredAutonomyDelivery | None
    transcript: StoredAutonomyConversationMessage | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class V2TelegramSpontaneityDeliveryResult:
    source_turn_id: str
    text: str
    telegram_message_id: int
    stored_delivery: StoredSpontaneityDelivery | None
    transcript: StoredSpontaneityConversationMessage | None
    warnings: tuple[str, ...] = ()


class V2TelegramDeliveryAdapter:
    """Explicit transport boundary for v2 user replies and assistant-initiated messages."""

    def __init__(
        self,
        *,
        gateway: TelegramGateway,
        conversation: V2ConversationCoordinator,
        autonomy: AutonomyRuntimeCoordinator,
        autonomy_message_generator: AutonomyMessageGenerator,
        sessions: ConversationSessionStore,
        preferences: SQLiteRuntimePreferenceStore | None = None,
        telemetry: SQLiteTurnTelemetryStore | None = None,
        spontaneity_store: SQLiteSpontaneityStore | None = None,
    ) -> None:
        self._gateway = gateway
        self._conversation = conversation
        self._autonomy = autonomy
        self._autonomy_message_generator = autonomy_message_generator
        self._sessions = sessions
        self._preferences = preferences
        self._telemetry = telemetry
        self._spontaneity_store = spontaneity_store

    async def deliver_user_message(
        self,
        message: IncomingMessage,
        *,
        requires_full_answer: bool = False,
    ) -> V2TelegramUserDeliveryResult:
        """Generate, externally send, then finalize exactly one inbound v2 user turn."""

        pre_warnings: list[str] = []
        if self._preferences is not None and message.text.strip():
            try:
                self._preferences.observe_user_text(
                    message.chat_id,
                    message.text,
                    at=message.received_at,
                )
            except Exception as exc:
                pre_warnings.append(self._warning("autonomy_sleep_state_failed", exc))

        delivery_started_at = datetime.now(UTC)
        queue_wait_ms = max(
            0,
            int(round((delivery_started_at - message.received_at).total_seconds() * 1000)),
        )
        delivery_started = perf_counter()
        time_to_send_ms = 0
        typing_task = asyncio.create_task(self._typing_loop(message.chat_id))
        try:
            user_images = await self._download_images(message)
            prepared = await self._conversation.prepare_user_turn(
                message.chat_id,
                self._canonical_user_text(message),
                at=message.received_at,
                requires_full_answer=requires_full_answer,
                user_images=user_images,
            )
            telegram_message_id = await self._gateway.send_message(
                message.chat_id,
                prepared.reply_text,
            )
            time_to_send_ms = self._elapsed_ms(delivery_started)
        finally:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task

        warnings: list[str] = list(pre_warnings)
        finalize: V2FinalizeResult | None = None
        finalize_started = perf_counter()
        try:
            finalize = await self._conversation.finalize_delivered_turn(
                prepared,
                delivered_assistant_text=prepared.reply_text,
                at=datetime.now(UTC),
            )
            warnings.extend(finalize.warnings)
            if finalize.exchange is None:
                warnings.append("delivered_turn_transcript_missing")
        except Exception as exc:
            warnings.append(self._warning("delivered_turn_finalize_failed", exc))
        finalize_ms = self._elapsed_ms(finalize_started)

        if self._telemetry is not None:
            try:
                self._record_user_turn_telemetry(
                    message=message,
                    prepared=prepared,
                    telegram_message_id=telegram_message_id,
                    finalize=finalize,
                    time_to_send_ms=time_to_send_ms,
                    finalize_ms=finalize_ms,
                    queue_wait_ms=queue_wait_ms,
                    warnings=tuple(warnings),
                )
            except Exception as exc:
                warnings.append(self._warning("turn_telemetry_failed", exc))

        return V2TelegramUserDeliveryResult(
            prepared=prepared,
            telegram_message_id=telegram_message_id,
            finalize=finalize,
            time_to_send_ms=time_to_send_ms,
            finalize_ms=finalize_ms,
            warnings=tuple(warnings),
        )

    async def prepare_autonomy_message(self, evaluation: AutonomyRuntimeEvaluation) -> str:
        """Generate proactive Character text without crossing the Telegram send boundary."""

        self._require_no_active_user_turn(evaluation.chat_id)
        self._autonomy.validate_delivery_candidate(evaluation)
        opportunity = evaluation.opportunity
        assert opportunity is not None
        response = await self._autonomy_message_generator.generate(
            decision=evaluation.decision,
            opportunity=opportunity,
            recent_conversation=self._sessions.history(evaluation.chat_id, 12),
        )
        self._require_no_active_user_turn(evaluation.chat_id)
        self._autonomy.validate_delivery_candidate(evaluation)
        return response.text

    async def deliver_prepared_autonomy_message(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        text: str,
    ) -> V2TelegramAutonomyDeliveryResult:
        """Send already-generated proactive text after a final race/staleness preflight."""

        normalized_text = text.strip()
        if not normalized_text:
            raise ValueError("prepared autonomy message must not be empty")
        self._require_no_active_user_turn(evaluation.chat_id)
        self._autonomy.validate_delivery_candidate(evaluation)
        telegram_message_id = await self._gateway.send_message(
            evaluation.chat_id,
            normalized_text,
        )
        delivered_at = datetime.now(UTC)

        selected_signal_id = evaluation.decision.selected_signal_id
        assert selected_signal_id is not None
        warnings: list[str] = []
        transcript: StoredAutonomyConversationMessage | None = None
        stored_delivery: StoredAutonomyDelivery | None = None
        try:
            transcript = self._sessions.append_v2_autonomy_message(
                evaluation.chat_id,
                normalized_text,
                generation=evaluation.generation,
                action=evaluation.decision.action.value,
                signal_id=selected_signal_id,
                telegram_message_id=telegram_message_id,
                at=delivered_at,
            )
        except Exception as exc:
            warnings.append(self._warning("autonomy_transcript_persistence_failed", exc))

        try:
            stored_delivery = self._autonomy.record_confirmed_external_delivery(
                evaluation,
                at=delivered_at,
            )
        except Exception as exc:
            warnings.append(self._warning("autonomy_delivery_stats_failed", exc))

        return V2TelegramAutonomyDeliveryResult(
            evaluation=evaluation,
            text=normalized_text,
            telegram_message_id=telegram_message_id,
            stored_delivery=stored_delivery,
            transcript=transcript,
            warnings=tuple(warnings),
        )

    async def deliver_spontaneity_message(
        self,
        *,
        chat_id: int,
        generation: int,
        source_turn_id: str,
        text: str,
        evaluation_id: int | None,
    ) -> V2TelegramSpontaneityDeliveryResult:
        """Send one already-authorized delayed continuation and persist it only after success."""

        normalized_text = text.strip()
        source = source_turn_id.strip()
        if not normalized_text:
            raise ValueError("prepared spontaneity message must not be empty")
        if not source:
            raise ValueError("spontaneity source_turn_id must not be empty")
        self._require_no_active_user_turn(chat_id)
        telegram_message_id = await self._gateway.send_message(chat_id, normalized_text)
        delivered_at = datetime.now(UTC)

        warnings: list[str] = []
        transcript: StoredSpontaneityConversationMessage | None = None
        stored_delivery: StoredSpontaneityDelivery | None = None
        try:
            transcript = self._sessions.append_v2_spontaneity_message(
                chat_id,
                normalized_text,
                generation=generation,
                source_turn_id=source,
                telegram_message_id=telegram_message_id,
                at=delivered_at,
            )
        except Exception as exc:
            warnings.append(self._warning("spontaneity_transcript_persistence_failed", exc))

        if self._spontaneity_store is not None:
            try:
                stored_delivery = self._spontaneity_store.record_delivery(
                    chat_id,
                    generation,
                    source_turn_id=source,
                    at=delivered_at,
                    evaluation_id=evaluation_id,
                )
            except Exception as exc:
                warnings.append(self._warning("spontaneity_delivery_stats_failed", exc))

        return V2TelegramSpontaneityDeliveryResult(
            source_turn_id=source,
            text=normalized_text,
            telegram_message_id=telegram_message_id,
            stored_delivery=stored_delivery,
            transcript=transcript,
            warnings=tuple(warnings),
        )

    async def deliver_autonomy_evaluation(
        self,
        evaluation: AutonomyRuntimeEvaluation,
    ) -> V2TelegramAutonomyDeliveryResult:
        """Generate and send one already-authorized proactive message."""

        text = await self.prepare_autonomy_message(evaluation)
        return await self.deliver_prepared_autonomy_message(evaluation, text)

    async def _download_images(self, message: IncomingMessage) -> tuple[LLMImage, ...]:
        images: list[LLMImage] = []
        for attachment in message.images:
            downloaded = await self._gateway.download_image(
                attachment,
                max_bytes=_MAX_PHOTO_BYTES,
            )
            images.append(LLMImage(data=downloaded.data, media_type=downloaded.media_type))
        return tuple(images)

    @staticmethod
    def _canonical_user_text(message: IncomingMessage) -> str:
        text = message.text.strip()
        if not message.images:
            return text
        if text:
            return f"{_PHOTO_MARKER}\nCaption: {text}"
        return _PHOTO_MARKER

    def _record_user_turn_telemetry(
        self,
        *,
        message: IncomingMessage,
        prepared: PreparedV2Turn,
        telegram_message_id: int,
        finalize: V2FinalizeResult | None,
        time_to_send_ms: int,
        finalize_ms: int,
        queue_wait_ms: int,
        warnings: tuple[str, ...],
    ) -> None:
        assert self._telemetry is not None
        exchange = finalize.exchange if finalize is not None else None
        timing = prepared.turn_result.timing
        response = prepared.turn_result.response
        usage = response.usage
        memory_enabled = (
            self._preferences.memory_enabled(message.chat_id)
            if self._preferences is not None
            else True
        )
        self._telemetry.record(
            TurnTelemetryRecord(
                turn_id=prepared.turn_id,
                chat_id=message.chat_id,
                user_message_id=exchange.user_message_id if exchange is not None else None,
                assistant_message_id=(
                    exchange.assistant_message_id if exchange is not None else None
                ),
                telegram_message_id=telegram_message_id,
                observed_at=datetime.now(UTC),
                policy_mode=timing.policy_mode,
                policy_act=prepared.turn_result.policy.act.value,
                policy_reason_label=prepared.turn_result.policy.reason_label,
                retrieval_ms=prepared.retrieval_ms,
                policy_ms=timing.policy_ms,
                context_ms=timing.context_ms,
                generation_ms=timing.generation_ms,
                character_total_ms=timing.total_ms,
                time_to_send_ms=time_to_send_ms,
                finalize_ms=finalize_ms,
                queue_wait_ms=queue_wait_ms,
                memory_enabled=memory_enabled,
                retrieved_memory_ids=prepared.retrieval.memory_ids,
                retrospective_triggered=(
                    finalize is not None and finalize.retrospective is not None
                ),
                warnings=warnings,
                generator_model=response.model,
                input_tokens=self._usage_value(usage, "input_tokens"),
                output_tokens=self._usage_value(usage, "output_tokens"),
                total_tokens=self._usage_value(usage, "total_tokens"),
            )
        )

    def _require_no_active_user_turn(self, chat_id: int) -> None:
        if self._conversation.has_active_turn(chat_id):
            raise ValueError("user turn active during assistant-initiated delivery")

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self._gateway.send_typing(chat_id)
            await asyncio.sleep(4)

    @staticmethod
    def _usage_value(usage: dict[str, int], key: str) -> int | None:
        return usage.get(key)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int(round((perf_counter() - started) * 1000)))

    @staticmethod
    def _warning(prefix: str, exc: Exception) -> str:
        return f"{prefix}:{type(exc).__name__}"
