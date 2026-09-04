from __future__ import annotations

import asyncio
import logging
from collections.abc import Set
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from amadeus_bot.runtime import (
    AutonomyRuntimeCoordinator,
    SQLiteRuntimePreferenceStore,
    V2ConversationCoordinator,
)

from .v2_delivery import V2TelegramDeliveryAdapter
from .v2_router import V2TelegramMessageRouter

LOGGER = logging.getLogger(__name__)
_CONTROL_CHANGE_COOLDOWN = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class AutonomyPilotTickSummary:
    considered_chats: int = 0
    evaluated_opportunities: int = 0
    non_silent_candidates: int = 0
    confirmed_deliveries: int = 0


class V2AutonomyPilotRunner:
    """Low-frequency in-process autonomy opportunity loop for the production pilot."""

    def __init__(
        self,
        *,
        autonomy: AutonomyRuntimeCoordinator,
        delivery: V2TelegramDeliveryAdapter,
        router: V2TelegramMessageRouter,
        conversation: V2ConversationCoordinator,
        preferences: SQLiteRuntimePreferenceStore,
        allowed_chat_ids: Set[int],
        timezone: str,
        wake_interval_seconds: float = 60.0,
    ) -> None:
        if not allowed_chat_ids or any(chat_id <= 0 for chat_id in allowed_chat_ids):
            raise ValueError("autonomy pilot requires positive allowed chat IDs")
        if wake_interval_seconds < 5 or wake_interval_seconds > 3600:
            raise ValueError("autonomy pilot wake interval must be between 5 and 3600 seconds")
        self._autonomy = autonomy
        self._delivery = delivery
        self._router = router
        self._conversation = conversation
        self._preferences = preferences
        self._allowed_chat_ids = frozenset(allowed_chat_ids)
        # Retain timezone validation for config compatibility even though fixed quiet hours
        # no longer gate proactive delivery in Phase 5.6.1.
        self._timezone = ZoneInfo(timezone)
        self._wake_interval_seconds = wake_interval_seconds

    async def run(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.tick()
            except Exception as exc:
                LOGGER.error("autonomy pilot tick failed type=%s", type(exc).__name__)
            await self._wait_or_stop(stop_event, self._wake_interval_seconds)

    async def tick(self, *, at: datetime | None = None) -> AutonomyPilotTickSummary:
        now = at or datetime.now(UTC)
        self._validate_aware(now)
        considered = 0
        evaluated = 0
        candidates = 0
        delivered = 0

        for chat_id in sorted(self._allowed_chat_ids):
            considered += 1
            preferences = self._preferences.autonomy_preferences(chat_id)
            if not preferences.enabled:
                continue
            if self._control_change_is_recent(preferences.updated_at, now):
                continue
            if self._conversation.has_active_turn(chat_id):
                continue

            try:
                evaluation = await self._autonomy.evaluate(
                    chat_id,
                    at=now,
                    do_not_disturb=preferences.do_not_disturb,
                    user_suppressed=False,
                    sleep_mode=preferences.sleep_mode,
                    sleep_started_at=preferences.sleep_started_at,
                )
            except Exception as exc:
                LOGGER.warning(
                    "autonomy evaluation failed chat_id=%s type=%s",
                    chat_id,
                    type(exc).__name__,
                )
                continue

            if not evaluation.due:
                continue
            evaluated += 1
            LOGGER.info(
                "autonomy evaluation chat_id=%s evaluation_id=%s action=%s reason=%s",
                chat_id,
                evaluation.evaluation_id,
                evaluation.decision.action.value,
                evaluation.decision.reason_label or "unspecified",
            )
            if not evaluation.should_prepare_delivery:
                continue
            candidates += 1

            try:
                text = await self._delivery.prepare_autonomy_message(evaluation)
                result = await self._router.deliver_prepared_autonomy(evaluation, text)
            except ValueError as exc:
                LOGGER.info(
                    "autonomy candidate abandoned chat_id=%s evaluation_id=%s reason=%s",
                    chat_id,
                    evaluation.evaluation_id,
                    str(exc),
                )
                continue
            except Exception as exc:
                LOGGER.warning(
                    "autonomy delivery failed chat_id=%s evaluation_id=%s type=%s",
                    chat_id,
                    evaluation.evaluation_id,
                    type(exc).__name__,
                )
                continue

            delivered += 1
            LOGGER.info(
                "autonomy delivered chat_id=%s evaluation_id=%s action=%s "
                "telegram_message_id=%s warnings=%s",
                chat_id,
                evaluation.evaluation_id,
                evaluation.decision.action.value,
                result.telegram_message_id,
                len(result.warnings),
            )

        return AutonomyPilotTickSummary(
            considered_chats=considered,
            evaluated_opportunities=evaluated,
            non_silent_candidates=candidates,
            confirmed_deliveries=delivered,
        )

    @staticmethod
    def _control_change_is_recent(updated_at: datetime | None, now: datetime) -> bool:
        if updated_at is None:
            return False
        if updated_at > now:
            return True
        return now - updated_at < _CONTROL_CHANGE_COOLDOWN

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("autonomy pilot timestamp must be timezone-aware")

    @staticmethod
    async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return
