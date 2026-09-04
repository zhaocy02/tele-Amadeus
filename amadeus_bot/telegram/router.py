from __future__ import annotations

import asyncio
import contextlib
import re

from amadeus_bot.memory import LegacyMemoryKind
from amadeus_bot.runtime import (
    ConversationBusyError,
    ConversationCancelledError,
    LegacyConversationService,
)

from .adapter import IncomingMessage, TelegramGateway

HELP_TEXT = "\n".join(
    (
        "Amadeus 已连接。直接发消息就好。",
        "",
        "/memory [on|off] - 查看或开关长期记忆",
        "/remember <内容> - 明确记住一件事",
        "/forget <编号|文字> - 删除记忆",
        "/profile - 查看保存的个人偏好",
        "/history [轮数] - 查看最近对话",
        "/new - 开始新对话（长期记忆保留）",
        "/status - 查看对话和记忆状态",
        "/cancel - 取消正在生成的回复",
        "/help - 显示帮助",
    )
)
_COMMAND_PATTERN = re.compile(r"^/([a-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.IGNORECASE)


class TelegramMessageRouter:
    def __init__(self, gateway: TelegramGateway, conversation: LegacyConversationService) -> None:
        self._gateway = gateway
        self._conversation = conversation

    async def handle(self, message: IncomingMessage) -> None:
        text = message.text.strip()
        command = self._parse_command(text)
        if command is not None:
            name, argument = command
            handled = await self._handle_command(message.chat_id, name, argument)
            if handled:
                return
        await self._handle_chat(message.chat_id, text)

    async def _handle_command(self, chat_id: int, name: str, argument: str) -> bool:
        if name in {"start", "help"}:
            await self._gateway.send_message(chat_id, HELP_TEXT)
            return True
        if name == "status":
            await self._gateway.send_message(chat_id, self._conversation.status_text(chat_id))
            return True
        if name == "new":
            try:
                self._conversation.start_new_conversation(chat_id)
            except ConversationBusyError as exc:
                await self._gateway.send_message(chat_id, str(exc))
            else:
                await self._gateway.send_message(
                    chat_id, "好，我们开始一段新对话。长期记忆仍会保留。"
                )
            return True
        if name == "cancel":
            cancelled = await self._conversation.cancel(chat_id)
            await self._gateway.send_message(
                chat_id,
                "已请求停止这次回复。" if cancelled else "当前没有正在生成的回复。",
            )
            return True
        if name == "history":
            turns = self._parse_history_turns(argument)
            if turns is None:
                await self._gateway.send_message(
                    chat_id, "用法：/history [轮数]，轮数范围为 1-20，默认 5。"
                )
            else:
                await self._gateway.send_message(
                    chat_id, self._conversation.history_text(chat_id, turns)
                )
            return True
        if name == "memory":
            await self._handle_memory(chat_id, argument)
            return True
        if name == "remember":
            await self._handle_remember(chat_id, argument)
            return True
        if name == "forget":
            await self._handle_forget(chat_id, argument)
            return True
        if name == "profile":
            items = self._conversation.memories(
                chat_id,
                20,
                (LegacyMemoryKind.PROFILE, LegacyMemoryKind.PREFERENCE),
            )
            await self._gateway.send_message(
                chat_id,
                self._conversation.format_memory_items(items)
                if items
                else "目前没有保存个人资料或偏好。",
            )
            return True
        return False

    async def _handle_memory(self, chat_id: int, argument: str) -> None:
        normalized = argument.strip().lower()
        if normalized in {"on", "off"}:
            try:
                self._conversation.set_memory_enabled(chat_id, normalized == "on")
            except ConversationBusyError as exc:
                await self._gateway.send_message(chat_id, str(exc))
                return
            await self._gateway.send_message(
                chat_id,
                "长期记忆已开启；相关记忆会用于之后的对话。"
                if normalized == "on"
                else "长期记忆已关闭；已有内容保留，但不会检索或自动新增。"
                "为清除当前上下文里的旧记忆，下一条消息会开始新对话。",
            )
            return
        if normalized:
            await self._gateway.send_message(chat_id, "用法：/memory、/memory on 或 /memory off")
            return
        status = "开启" if self._conversation.memory_enabled(chat_id) else "关闭"
        body = self._conversation.format_memory_items(self._conversation.memories(chat_id, 20))
        await self._gateway.send_message(chat_id, f"长期记忆：{status}\n\n{body}")

    async def _handle_remember(self, chat_id: int, argument: str) -> None:
        content = argument.strip()
        if not content:
            await self._gateway.send_message(chat_id, "用法：/remember <希望我记住的内容>")
            return
        try:
            item = self._conversation.remember(chat_id, content)
        except ValueError as exc:
            await self._gateway.send_message(chat_id, str(exc))
            return
        await self._gateway.send_message(chat_id, f"记住了（#{item.memory_id}）。")

    async def _handle_forget(self, chat_id: int, argument: str) -> None:
        selector = argument.strip()
        if not selector:
            body = self._conversation.format_memory_items(self._conversation.memories(chat_id, 20))
            await self._gateway.send_message(chat_id, f"用法：/forget <编号|文字>\n\n{body}")
            return
        try:
            removed = self._conversation.forget(chat_id, selector)
        except ConversationBusyError as exc:
            await self._gateway.send_message(chat_id, str(exc))
            return
        await self._gateway.send_message(
            chat_id,
            (
                f"已删除 {len(removed)} 条记忆："
                f"{'、'.join(f'#{item.memory_id}' for item in removed)}。"
                "下一条消息会开始新对话，避免旧上下文继续引用它。"
                if removed
                else "没有找到匹配的记忆。"
            ),
        )

    async def _handle_chat(self, chat_id: int, text: str) -> None:
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        try:
            reply = await self._conversation.reply(chat_id, text)
        except ConversationBusyError as exc:
            await self._gateway.send_message(chat_id, str(exc))
        except ConversationCancelledError as exc:
            await self._gateway.send_message(chat_id, str(exc))
        except Exception as exc:
            await self._gateway.send_message(chat_id, f"这次回复失败了：{exc}")
        else:
            await self._gateway.send_message(chat_id, reply)
        finally:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task

    async def _typing_loop(self, chat_id: int) -> None:
        while True:
            with contextlib.suppress(Exception):
                await self._gateway.send_typing(chat_id)
            await asyncio.sleep(4)

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str] | None:
        match = _COMMAND_PATTERN.match(text)
        if match is None:
            return None
        return match.group(1).lower(), (match.group(2) or "").strip()

    @staticmethod
    def _parse_history_turns(argument: str) -> int | None:
        if not argument.strip():
            return 5
        try:
            value = int(argument.strip())
        except ValueError:
            return None
        return value if 1 <= value <= 20 else None
