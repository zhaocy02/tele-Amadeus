from __future__ import annotations

from typing import Any

from amadeus_bot.runtime import AutonomyRuntimeEvaluation, RuntimeProviderControl

from .adapter import IncomingMessage
from .v2_delivery import V2TelegramDeliveryAdapter as _BaseV2TelegramDeliveryAdapter
from .v2_delivery import V2TelegramUserDeliveryResult


class ProviderAwareV2TelegramDeliveryAdapter(_BaseV2TelegramDeliveryAdapter):
    """Bind direct/autonomy generation to the selected chat provider."""

    def __init__(self, *, provider_control: RuntimeProviderControl, **kwargs: Any) -> None:
        self._provider_control = provider_control
        super().__init__(**kwargs)

    async def deliver_user_message(
        self,
        message: IncomingMessage,
        *,
        requires_full_answer: bool = False,
    ) -> V2TelegramUserDeliveryResult:
        with self._provider_control.use_chat(message.chat_id):
            return await super().deliver_user_message(
                message,
                requires_full_answer=requires_full_answer,
            )

    async def prepare_autonomy_message(self, evaluation: AutonomyRuntimeEvaluation) -> str:
        with self._provider_control.use_chat(evaluation.chat_id):
            return await super().prepare_autonomy_message(evaluation)
