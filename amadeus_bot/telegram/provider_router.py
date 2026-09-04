from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from amadeus_bot.runtime import AutonomyTuning, RuntimeAutonomyTuningControl, RuntimeProviderControl

from .adapter import IncomingMessage
from .v2_delivery import V2TelegramUserDeliveryResult
from .v2_observation_router import V2TelegramMessageRouter as _ObservationRouter
from .v2_status_router import V2_HELP_TEXT as _BASE_HELP_TEXT

V2_HELP_TEXT = _BASE_HELP_TEXT.replace(
    "/help - 显示帮助",
    "/provider [cpa|deepseek|web ...] - 查看或切换模型/搜索上游\n/help - 显示帮助",
)
_NATURAL_SWITCH_CLEAN_RE = re.compile(r"[\s。！!？?，,、]+")
_TUNE_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")
_NATURAL_LLM_SWITCHES = {
    "切到deepseek": "deepseek",
    "切换到deepseek": "deepseek",
    "换成deepseek": "deepseek",
    "改用deepseek": "deepseek",
    "用deepseek": "deepseek",
    "使用deepseek": "deepseek",
    "切到ds": "deepseek",
    "换成ds": "deepseek",
    "切回codex": "cpa",
    "换回codex": "cpa",
    "用回codex": "cpa",
    "改回codex": "cpa",
    "切到codex": "cpa",
    "切回cpa": "cpa",
    "换回cpa": "cpa",
    "切到cpa": "cpa",
}


class V2TelegramMessageRouter(_ObservationRouter):
    """Add local provider/tuning controls without making Character generation the control plane."""

    def __init__(
        self,
        *,
        provider_control: RuntimeProviderControl | None = None,
        autonomy_tuning: RuntimeAutonomyTuningControl | None = None,
        **kwargs: Any,
    ) -> None:
        self._provider_control = provider_control
        self._autonomy_tuning = autonomy_tuning
        super().__init__(**kwargs)

    async def handle(self, message: IncomingMessage) -> None:
        text = message.text.strip()
        command = self._parse_command(text)
        normalized = self._normalize_command(*command) if command is not None else None

        if (
            self._provider_control is not None
            and normalized is not None
            and normalized[0] == "provider"
        ):
            await self._handle_provider_control(
                message,
                normalized[1],
                cancel_active=self._provider_command_mutates(normalized[1]),
            )
            return

        natural = self._natural_llm_switch(text) if self._provider_control is not None else None
        if natural is not None:
            await self._handle_provider_control(
                message,
                natural,
                cancel_active=True,
            )
            return

        await super().handle(message)

    async def _handle_command(self, message: Any, name: str, argument: str) -> bool:
        if name in {"start", "help"} and self._provider_control is not None:
            await self._send_command_text(message.chat_id, V2_HELP_TEXT)
            return True
        return await super()._handle_command(message, name, argument)

    async def _handle_autonomy(self, chat_id: int, argument: str) -> None:
        normalized = " ".join(argument.strip().lower().split())
        if normalized in {"", "status"}:
            if self._autonomy_runtime is not None:
                text = await self._autonomy_live_status_text(chat_id)
            else:
                text = self._autonomy_status_text(chat_id)
                text = text.replace(
                    "policy=opportunity:15m idle:30m base_cooldown:30m max:12/24h unanswered:3",
                    "policy=opportunity:5m idle:3m base_cooldown:30m max:24/24h unanswered:3",
                ).replace(
                    "spontaneity_policy=delay:6-50s cooldown:2m max:18/24h one-per-source-turn",
                    "spontaneity_policy=delay:6-50s cooldown:2m max:unlimited one-per-source-turn",
                )
            text = self._normalize_unlimited_status(text)
            if self._autonomy_tuning is not None:
                tuning = self._autonomy_tuning.get(chat_id)
                if tuning.spontaneity_max_messages_per_24h is not None:
                    text = text.replace(
                        "max:unlimited",
                        f"max:{tuning.spontaneity_max_messages_per_24h}/24h",
                    )
                text += "\n" + self._autonomy_tuning_compact_text(tuning)
            await self._send_command_text(chat_id, text)
            return
        if normalized.startswith("tune"):
            await self._send_command_text(chat_id, self._autonomy_tune_text(chat_id, normalized))
            return
        if normalized in {"on", "off"}:
            enabled = normalized == "on"
            self._preferences.set_autonomy_enabled(chat_id, enabled)
            if enabled and not self._autonomy_pilot_enabled:
                text = (
                    "已保存主动消息 opt-in，但当前进程级 gate 仍关闭；"
                    "在运行时 gate 开启前不会主动发送。"
                )
            elif enabled:
                tuning = (
                    self._autonomy_tuning.get(chat_id)
                    if self._autonomy_tuning is not None
                    else AutonomyTuning()
                )
                text = (
                    f"主动消息已开启：沉默 {self._format_duration(tuning.min_user_idle)} "
                    "后可开始主动思考，"
                    f"每 {self._format_duration(tuning.check_interval)} 最多评估一次；"
                    f"基础 cooldown 30 分钟，24 小时最多 {tuning.max_messages_per_24h} 条，"
                    "连续 3 条未回复后暂停。"
                )
            else:
                text = "主动消息已关闭；long-horizon autonomy 与短期续话都会停止发送。"
            await self._send_command_text(chat_id, text)
            return
        await super()._handle_autonomy(chat_id, argument)

    def _autonomy_tune_text(self, chat_id: int, normalized: str) -> str:
        control = self._autonomy_tuning
        if control is None:
            return "当前 runtime 未启用 autonomy 热调控制。"
        parts = normalized.split()
        if parts in (["tune"], ["tune", "status"]):
            return self._autonomy_tuning_status_text(control.get(chat_id))
        if parts == ["tune", "reset"]:
            tuning = control.reset(chat_id)
            return "已恢复当前生产默认值；立即生效。\n" + self._autonomy_tuning_status_text(tuning)
        if len(parts) != 3 or parts[0] != "tune":
            return self._autonomy_tune_usage()

        key, raw_value = parts[1], parts[2]
        try:
            if key == "idle":
                tuning = control.set_min_user_idle(chat_id, self._parse_tune_duration(raw_value))
            elif key in {"interval", "cadence"}:
                tuning = control.set_check_interval(chat_id, self._parse_tune_duration(raw_value))
            elif key == "daily":
                tuning = control.set_daily_cap(chat_id, self._parse_positive_int(raw_value))
            elif key == "drive":
                tuning = control.set_idle_drive_bonus(chat_id, float(raw_value))
            elif key in {"spontaneous", "spontaneity"}:
                cap = None if raw_value == "unlimited" else self._parse_positive_int(raw_value)
                tuning = control.set_spontaneity_daily_cap(chat_id, cap)
            else:
                return self._autonomy_tune_usage()
        except ValueError as exc:
            return f"参数无效：{exc}"
        return "已热更新并持久化；无需重启。\n" + self._autonomy_tuning_status_text(tuning)

    @staticmethod
    def _autonomy_tune_usage() -> str:
        return (
            "用法：/autonomy tune、/autonomy tune idle 5m、"
            "/autonomy tune interval 10m、/autonomy tune daily 18、"
            "/autonomy tune drive 0.20、/autonomy tune spontaneous unlimited|N、"
            "/autonomy tune reset"
        )

    @classmethod
    def _autonomy_tuning_status_text(cls, tuning: AutonomyTuning) -> str:
        spontaneous = (
            "unlimited"
            if tuning.spontaneity_max_messages_per_24h is None
            else str(tuning.spontaneity_max_messages_per_24h)
        )
        return "\n".join(
            (
                "Autonomy 热调（当前 chat，持久化）",
                f"idle={cls._format_duration(tuning.min_user_idle)}",
                f"interval={cls._format_duration(tuning.check_interval)}",
                f"daily={tuning.max_messages_per_24h}",
                f"drive={tuning.idle_drive_max_motivation_bonus:.2f}",
                f"spontaneous={spontaneous}",
                f"customized={'on' if tuning.customized else 'off'}",
                "固定安全阀：base cooldown=30m；unanswered=3；sleep cap=2；salient signal 仍必需。",
            )
        )

    @classmethod
    def _autonomy_tuning_compact_text(cls, tuning: AutonomyTuning) -> str:
        spontaneous = (
            "unlimited"
            if tuning.spontaneity_max_messages_per_24h is None
            else str(tuning.spontaneity_max_messages_per_24h)
        )
        return (
            "hot_tune="
            f"idle:{cls._format_duration(tuning.min_user_idle)} "
            f"interval:{cls._format_duration(tuning.check_interval)} "
            f"daily:{tuning.max_messages_per_24h} "
            f"drive:{tuning.idle_drive_max_motivation_bonus:.2f} "
            f"spontaneous:{spontaneous}"
        )

    @staticmethod
    def _parse_tune_duration(value: str) -> timedelta:
        match = _TUNE_DURATION_RE.fullmatch(value)
        if match is None:
            raise ValueError("时间必须写成 5m、2h 或 1d")
        amount = float(match.group(1))
        unit = match.group(2)
        if amount <= 0:
            raise ValueError("时间必须大于 0")
        if unit == "m":
            return timedelta(minutes=amount)
        if unit == "h":
            return timedelta(hours=amount)
        return timedelta(days=amount)

    @staticmethod
    def _parse_positive_int(value: str) -> int:
        if not value.isdigit():
            raise ValueError("数量必须是正整数")
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("数量必须是正整数")
        return parsed

    @staticmethod
    def _format_duration(value: timedelta) -> str:
        seconds = int(value.total_seconds())
        if seconds % 86400 == 0:
            return f"{seconds // 86400}d"
        if seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        if seconds % 60 == 0:
            return f"{seconds // 60}m"
        return f"{seconds}s"

    def _spontaneity_preflight(
        self,
        chat_id: int,
        *,
        generation: int,
        source_user_message_id: int,
    ) -> bool:
        if not super()._spontaneity_preflight(
            chat_id,
            generation=generation,
            source_user_message_id=source_user_message_id,
        ):
            return False
        control = self._autonomy_tuning
        store = self._spontaneity_store
        if control is None or store is None:
            return True
        cap = control.get(chat_id).spontaneity_max_messages_per_24h
        if cap is None:
            return True
        stats = store.delivery_stats(chat_id, generation, now=datetime.now(UTC))
        return stats.messages_last_24h < cap

    async def _handle_provider_control(
        self,
        message: IncomingMessage,
        argument: str,
        *,
        cancel_active: bool,
    ) -> None:
        control = self._provider_control
        assert control is not None
        chat_id = message.chat_id
        if cancel_active:
            self._cancel_spontaneity(chat_id)
            if self._conversation.has_active_turn(chat_id):
                await self._conversation.cancel(chat_id)

        async with self._chat_lock(chat_id):
            await self._send_command_text(
                chat_id,
                self._provider_command_text(chat_id, argument),
            )

    def _provider_command_text(self, chat_id: int, argument: str) -> str:
        control = self._provider_control
        assert control is not None
        normalized = " ".join(argument.strip().casefold().split())
        if normalized in {"", "status"}:
            return self._provider_status_text(chat_id)

        parts = normalized.split()
        try:
            if len(parts) == 1:
                selected = control.set_llm_provider(chat_id, parts[0])
                status = control.status(chat_id)
                return (
                    f"已切换 LLM provider：{selected}。\n"
                    f"文字模型：{status.text_model}\n"
                    f"图片模型：{status.vision_model or '不支持'}\n"
                    "Persona、长期记忆、Character State 和对话历史不会重置。"
                )
            if len(parts) == 2 and parts[0] in {"llm", "model"}:
                selected = control.set_llm_provider(chat_id, parts[1])
                status = control.status(chat_id)
                return (
                    f"已切换 LLM provider：{selected}。\n"
                    f"文字模型：{status.text_model}\n"
                    f"图片模型：{status.vision_model or '不支持'}"
                )
            if len(parts) == 2 and parts[0] in {"web", "search"}:
                selected = control.set_web_search_provider(chat_id, parts[1])
                return f"已切换 Web Search provider：{selected}。"
        except ValueError as exc:
            return str(exc)

        return (
            "用法：/provider、/provider cpa、/provider deepseek、"
            "/provider web cpa、/provider web deepseek"
        )

    def _provider_status_text(self, chat_id: int) -> str:
        control = self._provider_control
        assert control is not None
        status = control.status(chat_id)
        available_llm = ", ".join(control.llm_registry.available_providers)
        web_registry = control.web_search_registry
        available_web = (
            "disabled"
            if web_registry is None
            else ", ".join(web_registry.available_providers)
        )
        lines = [
            "Amadeus provider",
            f"LLM={status.llm_provider} ({status.llm_label})",
            f"text_model={status.text_model}",
            f"vision_model={status.vision_model or 'none'}",
            f"available_llm={available_llm}",
            f"web_search={status.web_search_provider or 'disabled'}",
            f"available_web_search={available_web}",
            "automatic_failover=off",
        ]
        if status.llm_fell_back_to_default:
            lines.append(
                f"warning=requested_llm_unavailable:{status.requested_llm_provider}"
            )
        if status.web_search_fell_back_to_default:
            lines.append(
                "warning=requested_web_search_unavailable:"
                f"{status.requested_web_search_provider}"
            )
        return "\n".join(lines)

    async def _status_text(self, chat_id: int) -> str:
        base = self._normalize_unlimited_status(await super()._status_text(chat_id))
        control = self._provider_control
        if control is None:
            return base
        status = control.status(chat_id)
        lines = [
            base,
            "",
            "模型",
            f"  LLM：{status.llm_label}",
            f"  文字：{status.text_model}",
            f"  图片：{status.vision_model or '当前 provider 不支持'}",
            f"  Web Search：{status.web_search_provider or '关闭'}",
        ]
        return "\n".join(lines)

    async def _debug_status_text(self, chat_id: int) -> str:
        base = self._normalize_unlimited_status(await super()._debug_status_text(chat_id))
        tuning_control = self._autonomy_tuning
        if tuning_control is not None:
            base += "\n" + self._autonomy_tuning_compact_text(tuning_control.get(chat_id))
        control = self._provider_control
        if control is None:
            return base
        status = control.status(chat_id)
        return "\n".join(
            (
                base,
                f"selected_llm_provider={status.llm_provider}",
                f"selected_text_model={status.text_model}",
                f"selected_vision_model={status.vision_model or 'none'}",
                f"selected_web_search_provider={status.web_search_provider or 'disabled'}",
                "automatic_provider_failover=off",
            )
        )

    def _schedule_spontaneity(
        self,
        chat_id: int,
        result: V2TelegramUserDeliveryResult,
    ) -> None:
        control = self._provider_control
        if control is None:
            super()._schedule_spontaneity(chat_id, result)
            return
        # asyncio.create_task captures contextvars, so the delayed continuation keeps the
        # provider selection even after this short context manager exits.
        with control.use_chat(chat_id):
            super()._schedule_spontaneity(chat_id, result)

    @staticmethod
    def _normalize_unlimited_status(text: str) -> str:
        return text.replace("max:inf/24h", "max:unlimited").replace(" / inf", " 条")

    @staticmethod
    def _provider_command_mutates(argument: str) -> bool:
        normalized = " ".join(argument.strip().casefold().split())
        return normalized not in {"", "status"}

    @staticmethod
    def _natural_llm_switch(text: str) -> str | None:
        if not text or len(text) > 40:
            return None
        normalized = _NATURAL_SWITCH_CLEAN_RE.sub("", text.casefold())
        return _NATURAL_LLM_SWITCHES.get(normalized)
