from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from amadeus_bot.runtime.autonomy_runtime import AutonomyRuntimePreview

from .adapter import IncomingMessage
from .v2_status_router import V2_HELP_TEXT as V2_HELP_TEXT
from .v2_status_router import V2TelegramMessageRouter as _HumanStatusRouter


class V2TelegramMessageRouter(_HumanStatusRouter):
    """Keep read-only status observation from perturbing spontaneous behavior."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._preview_errors: dict[int, str] = {}

    async def handle(self, message: IncomingMessage) -> None:
        text = message.text.strip()
        command = self._parse_command(text)
        normalized = self._normalize_command(*command) if command is not None else None

        if normalized is not None and self._is_observation_command(*normalized):
            # Observation commands do not represent new conversational activity. In particular,
            # they must not cancel an uncommitted spontaneous continuation or increment the
            # pending-user counter that its preflight checks. The shared chat lock still keeps
            # the status response ordered against a continuation that already committed to send.
            async with self._chat_lock(message.chat_id):
                await self._handle_command(message, *normalized)
            return

        await super().handle(message)

    async def _preview(
        self,
        chat_id: int,
        *,
        now: datetime,
    ) -> AutonomyRuntimePreview | None:
        runtime = self._autonomy_runtime
        if runtime is None:
            self._preview_errors.pop(chat_id, None)
            return None
        preferences = self._preferences.autonomy_preferences(chat_id)
        try:
            preview = await runtime.preview(
                chat_id,
                at=now,
                do_not_disturb=preferences.do_not_disturb,
                sleep_mode=preferences.sleep_mode,
                sleep_started_at=(
                    preferences.sleep_started_at if preferences.sleep_mode else None
                ),
            )
        except ValueError as exc:
            self._preview_errors[chat_id] = self._compact_text(str(exc), 180)
            return None
        self._preview_errors.pop(chat_id, None)
        return preview

    async def _status_text(self, chat_id: int) -> str:
        text = await super()._status_text(chat_id)
        text = text.replace(
            "想主动找你：暂时无法计算",
            "主动联系倾向：暂时无法计算",
            1,
        )
        if "autonomy preview 不可用" not in text:
            return text
        last_user = self._sessions.last_user_message_at(chat_id)
        if last_user is None:
            return text
        replacement = "距离你上次说话：" + self._since_text(last_user, now=datetime.now(UTC))
        return text.replace("距离你上次说话：暂无", replacement, 1)

    def _human_autonomy_lines(
        self,
        chat_id: int,
        preview: AutonomyRuntimePreview,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        base = list(super()._human_autonomy_lines(chat_id, preview, now=now))
        strongest_salience = (
            0.0 if preview.strongest_signal is None else preview.strongest_signal.salience
        )
        composite = base[0].replace("想主动找你：", "主动联系倾向：", 1)
        composite = composite.replace("（倾向，不是发送概率）", "（综合倾向，不是发送概率）")
        return (
            composite,
            (
                "  想联系你："
                f"{self._bar(preview.idle_contact_drive)} {preview.idle_contact_drive:.0%}"
                "（主要来自沉默时间）"
            ),
            (
                "  有话想说："
                f"{self._bar(strongest_salience)} {strongest_salience:.0%}"
            ),
            *base[2:],
        )

    async def _debug_status_text(self, chat_id: int) -> str:
        text = await super()._debug_status_text(chat_id)
        error = self._preview_errors.get(chat_id)
        if error is None:
            return text
        return text + "\nautonomy_preview_error=" + error

    @staticmethod
    def _is_observation_command(name: str, argument: str) -> bool:
        if name == "status":
            return True
        if name != "autonomy":
            return False
        normalized = " ".join(argument.strip().lower().split())
        return normalized in {"", "status"}
