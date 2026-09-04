from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

import httpx

from .adapter import DownloadedImage, IncomingImageAttachment
from .command_menu import V2_BOT_COMMANDS

LOGGER = logging.getLogger(__name__)
_COMMAND_MENU_SYNC_MAX_ATTEMPTS = 3


class TelegramTransportError(RuntimeError):
    """Sanitized Telegram transport failure that never includes the tokenized request URL."""


class TelegramHTTPGateway:
    def __init__(
        self,
        *,
        bot_token: str,
        api_base_url: str = "https://api.telegram.org",
        proxy_url: str | None = None,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._api_base_url = api_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            proxy=proxy_url,
            trust_env=False,
        )
        self._command_menu_synced = False
        self._command_menu_sync_attempts = 0

    async def send_message(self, chat_id: int, text: str) -> int:
        data = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        result = data.get("result")
        if not isinstance(result, Mapping):
            raise TelegramTransportError("Telegram sendMessage returned no result")
        message_id = result.get("message_id")
        if not isinstance(message_id, int):
            raise TelegramTransportError("Telegram sendMessage returned no message ID")
        return message_id

    async def send_typing(self, chat_id: int) -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    async def get_me(self) -> Mapping[str, object]:
        data = await self._call("getMe", {})
        result = data.get("result")
        if not isinstance(result, Mapping):
            raise TelegramTransportError("Telegram getMe returned no result")
        return result

    async def set_my_commands(self, commands: Sequence[tuple[str, str]]) -> None:
        await self._call(
            "setMyCommands",
            {
                "commands": [
                    {"command": command, "description": description}
                    for command, description in commands
                ]
            },
        )

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 25,
    ) -> tuple[Mapping[str, object], ...]:
        if not 0 <= timeout_seconds <= 50:
            raise ValueError("Telegram long-poll timeout must be between 0 and 50 seconds")
        await self._try_sync_command_menu()
        payload: dict[str, object] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        data = await self._call("getUpdates", payload)
        result = data.get("result")
        if not isinstance(result, list):
            raise TelegramTransportError("Telegram getUpdates returned no result list")
        updates: list[Mapping[str, object]] = []
        for item in result:
            if not isinstance(item, Mapping):
                raise TelegramTransportError("Telegram getUpdates returned a malformed update")
            updates.append(item)
        return tuple(updates)

    async def download_image(
        self,
        attachment: IncomingImageAttachment,
        *,
        max_bytes: int,
    ) -> DownloadedImage:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if attachment.byte_size_hint is not None and attachment.byte_size_hint > max_bytes:
            raise TelegramTransportError("Telegram photo exceeds the configured byte limit")

        data = await self._call("getFile", {"file_id": attachment.source_id})
        result = data.get("result")
        if not isinstance(result, Mapping):
            raise TelegramTransportError("Telegram getFile returned no result")
        file_path = result.get("file_path")
        file_size = result.get("file_size")
        if not isinstance(file_path, str) or not self._safe_file_path(file_path):
            raise TelegramTransportError("Telegram getFile returned an invalid file path")
        if isinstance(file_size, int) and file_size > max_bytes:
            raise TelegramTransportError("Telegram photo exceeds the configured byte limit")

        url = f"{self._api_base_url}/file/bot{self._bot_token}/{file_path}"
        try:
            async with self._client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise TelegramTransportError(
                        f"Telegram file download returned HTTP {response.status_code}"
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > max_bytes:
                            raise TelegramTransportError(
                                "Telegram photo exceeds the configured byte limit"
                            )
                    except ValueError:
                        pass
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise TelegramTransportError(
                            "Telegram photo exceeds the configured byte limit"
                        )
        except TelegramTransportError:
            raise
        except httpx.TimeoutException:
            raise TelegramTransportError("Telegram file download timed out") from None
        except httpx.HTTPError:
            raise TelegramTransportError("Telegram file download failed") from None

        if not payload:
            raise TelegramTransportError("Telegram file download returned an empty image")
        return DownloadedImage(
            data=bytes(payload),
            media_type=self._media_type(file_path, attachment.media_type),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _try_sync_command_menu(self) -> None:
        if self._command_menu_synced:
            return
        if self._command_menu_sync_attempts >= _COMMAND_MENU_SYNC_MAX_ATTEMPTS:
            return
        self._command_menu_sync_attempts += 1
        try:
            await self.set_my_commands(V2_BOT_COMMANDS)
        except TelegramTransportError as exc:
            LOGGER.warning(
                "Telegram command menu sync failed attempt=%s/%s: %s",
                self._command_menu_sync_attempts,
                _COMMAND_MENU_SYNC_MAX_ATTEMPTS,
                exc,
            )
        else:
            self._command_menu_synced = True
            LOGGER.info("Telegram command menu synced commands=%s", len(V2_BOT_COMMANDS))

    async def _call(self, method: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        url = f"{self._api_base_url}/bot{self._bot_token}/{method}"
        try:
            response = await self._client.post(url, json=dict(payload))
        except httpx.TimeoutException:
            raise TelegramTransportError(f"Telegram {method} timed out") from None
        except httpx.HTTPError:
            raise TelegramTransportError(f"Telegram {method} request failed") from None
        if response.status_code >= 400:
            raise TelegramTransportError(f"Telegram {method} returned HTTP {response.status_code}")
        raw: object = response.json()
        if not isinstance(raw, dict) or raw.get("ok") is not True:
            raise TelegramTransportError(f"Telegram {method} returned an invalid response")
        return raw

    @staticmethod
    def _safe_file_path(value: str) -> bool:
        path = PurePosixPath(value)
        return bool(value) and not value.startswith("/") and ".." not in path.parts

    @staticmethod
    def _media_type(file_path: str, fallback: str) -> str:
        normalized = file_path.casefold()
        if normalized.endswith(".png"):
            return "image/png"
        if normalized.endswith(".webp"):
            return "image/webp"
        if normalized.endswith(".gif"):
            return "image/gif"
        if normalized.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        return fallback if fallback.startswith("image/") else "image/jpeg"
