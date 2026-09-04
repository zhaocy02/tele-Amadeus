from __future__ import annotations

from typing import Any

from .adapter import IncomingMessage
from .github_feedback_router import GitHubFeedbackCommandRouter
from .provider_router import V2_HELP_TEXT as _BASE_HELP_TEXT
from .spontaneity_episode_router import V2TelegramMessageRouter as _SpontaneityRouter

V2_HELP_TEXT = _BASE_HELP_TEXT.replace(
    "/help - 显示帮助",
    "/github - public GitHub feedback proposal/confirm\n/help - 显示帮助",
)


class V2TelegramMessageRouter(_SpontaneityRouter):
    """Add explicit GitHub feedback control outside the full v2 Telegram router stack."""

    def __init__(
        self,
        *,
        github_feedback: GitHubFeedbackCommandRouter | None = None,
        **kwargs: Any,
    ) -> None:
        self._github_feedback = github_feedback
        super().__init__(**kwargs)

    async def handle(self, message: IncomingMessage) -> None:
        command = self._parse_command(message.text.strip())
        normalized = self._normalize_command(*command) if command is not None else None
        if normalized is not None and normalized[0] == "github":
            self._cancel_spontaneity(message.chat_id)
            async with self._chat_lock(message.chat_id):
                if self._github_feedback is None:
                    await self._send_command_text(
                        message.chat_id,
                        "Public GitHub Feedback 当前未启用。",
                    )
                else:
                    await self._github_feedback.handle(message.chat_id, normalized[1])
            return
        await super().handle(message)

    async def _handle_command(self, message: Any, name: str, argument: str) -> bool:
        if name in {"start", "help"}:
            await self._send_command_text(message.chat_id, V2_HELP_TEXT)
            return True
        return await super()._handle_command(message, name, argument)
