from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from amadeus_bot.runtime.autonomy_runtime import (
    AutonomyRuntimeCoordinator,
    AutonomyRuntimePreview,
)

from .v2_router import V2_HELP_TEXT as _BASE_HELP_TEXT
from .v2_router import V2TelegramMessageRouter as _BaseV2TelegramMessageRouter

V2_HELP_TEXT = _BASE_HELP_TEXT.replace(
    "/status - 查看 v2 对话、记忆和最近延迟",
    "/status [debug] - 查看角色状态；debug 显示机器诊断",
)


class V2TelegramMessageRouter(_BaseV2TelegramMessageRouter):
    """V2 router with a human-facing status dashboard and explicit debug view."""

    def __init__(
        self,
        *,
        autonomy: AutonomyRuntimeCoordinator | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._autonomy_runtime = autonomy

    async def _handle_command(self, message: Any, name: str, argument: str) -> bool:
        chat_id = message.chat_id
        if name in {"start", "help"}:
            await self._send_command_text(chat_id, V2_HELP_TEXT)
            return True
        if name == "status":
            normalized = argument.strip().casefold()
            if normalized in {"", "human"}:
                text = await self._status_text(chat_id)
            elif normalized == "debug":
                text = await self._debug_status_text(chat_id)
            else:
                text = "用法：/status 或 /status debug"
            await self._send_command_text(chat_id, text)
            return True
        return await super()._handle_command(message, name, argument)

    async def _handle_autonomy(self, chat_id: int, argument: str) -> None:
        normalized = " ".join(argument.strip().lower().split())
        if normalized in {"", "status"} and self._autonomy_runtime is not None:
            await self._send_command_text(chat_id, await self._autonomy_live_status_text(chat_id))
            return
        await super()._handle_autonomy(chat_id, argument)

    async def _status_text(self, chat_id: int) -> str:
        if self._autonomy_runtime is None:
            return await super()._status_text(chat_id)

        now = datetime.now(UTC)
        state = await self._state.load_state()
        active_memories = await self._memory.list_active(limit=500)
        preferences = self._preferences.autonomy_preferences(chat_id)
        preview = await self._preview(chat_id, now=now)
        last_user = None if preview is None else preview.opportunity.last_user_message_at

        lines = [
            "Amadeus 状态",
            "",
            "对话",
            f"  已聊 {self._sessions.v2_turn_count(chat_id)} 轮",
            f"  当前：{'正在回复你' if self._conversation.has_active_turn(chat_id) else '空闲'}",
            "  距离你上次说话：" + self._since_text(last_user, now=now),
            (
                f"  长期记忆：{'开启' if self._preferences.memory_enabled(chat_id) else '关闭'}"
                f" · {len(active_memories)} 条"
            ),
            f"  Character State：v{state.state_version if state is not None else 0}",
            "",
            "主动性",
            f"  主动联系：{self._switch_text(preferences.enabled, self._autonomy_pilot_enabled)}",
            (
                "  短期续话："
                + self._switch_text(
                    preferences.spontaneity_enabled,
                    self._spontaneity_process_enabled,
                )
            ),
        ]

        if preview is None:
            lines.extend(("  想主动找你：暂时无法计算", "  当前原因：autonomy preview 不可用"))
        else:
            lines.extend(self._human_autonomy_lines(chat_id, preview, now=now))

        short_stats = None
        if self._spontaneity_store is not None:
            short_stats = self._spontaneity_store.delivery_stats(
                chat_id,
                self._sessions.current_generation(chat_id),
                now=now,
            )
        short_limit = (
            self._spontaneity_composer.config.max_messages_per_24h
            if self._spontaneity_composer is not None
            else None
        )
        short_state = (
            "有一条续话正在形成/等待"
            if chat_id in self._spontaneity_tasks
            else "没有待发送续话"
        )
        short_count = short_stats.messages_last_24h if short_stats else 0
        lines.extend(
            (
                "",
                "短期 spontaneous",
                "  当前：" + short_state,
                self._short_usage_line(short_count, short_limit),
                "",
                "机器诊断：/status debug",
            )
        )
        return "\n".join(lines)

    async def _debug_status_text(self, chat_id: int) -> str:
        lines = [(await super()._status_text(chat_id)), "", "human_dashboard_debug=on"]
        if self._autonomy_runtime is not None:
            now = datetime.now(UTC)
            preview = await self._preview(chat_id, now=now)
            if preview is not None:
                strongest = preview.strongest_signal
                lines.extend(
                    (
                        f"autonomy_idle_contact_drive={preview.idle_contact_drive:.4f}",
                        f"autonomy_contact_urge={preview.contact_urge:.4f}",
                        (
                            "autonomy_raw_motivation_threshold="
                            f"{preview.raw_motivation_threshold:.4f}"
                        ),
                        f"autonomy_guard_allowed={'yes' if preview.guard_allowed else 'no'}",
                        f"autonomy_guard_reason={preview.guard_reason_label}",
                        f"autonomy_next_opportunity_at={preview.next_opportunity_at.isoformat()}",
                        (
                            "autonomy_strongest_signal="
                            + ("none" if strongest is None else strongest.signal_id)
                        ),
                        (
                            "autonomy_strongest_salience="
                            + ("0.0000" if strongest is None else f"{strongest.salience:.4f}")
                        ),
                    )
                )
        config = self._spontaneity_composer.config if self._spontaneity_composer else None
        if config is not None:
            lines.extend(
                (
                    (
                        "spontaneity_live_policy="
                        f"delay:{config.min_delay.total_seconds():g}-"
                        f"{config.max_delay.total_seconds():g}s "
                        f"cooldown:{config.cooldown.total_seconds() / 60:g}m "
                        f"max:{config.max_messages_per_24h}/24h"
                    ),
                    (
                        "spontaneity_live_sampling="
                        f"direct:{config.direct_answer_sample_rate:.2f} "
                        f"short:{config.short_answer_sample_rate:.2f} "
                        f"interrupt:{config.interrupt_sample_rate:.2f} "
                        f"grace:{config.interrupt_grace_seconds:g}s"
                    ),
                )
            )
        return "\n".join(lines)

    async def _autonomy_live_status_text(self, chat_id: int) -> str:
        now = datetime.now(UTC)
        preferences = self._preferences.autonomy_preferences(chat_id)
        preview = await self._preview(chat_id, now=now)
        lines = [
            "Amadeus 主动消息",
            f"chat_opt_in={'on' if preferences.enabled else 'off'}",
            f"runtime_gate={'on' if self._autonomy_pilot_enabled else 'off'}",
            f"spontaneity_chat={'on' if preferences.spontaneity_enabled else 'off'}",
            f"spontaneity_runtime_gate={'on' if self._spontaneity_process_enabled else 'off'}",
            f"dnd={'on' if preferences.do_not_disturb else 'off'}",
            f"sleep_mode={'on' if preferences.sleep_mode else 'off'}",
            "sleep_trigger=晚安/哦呀斯密 -> 早上好",
        ]
        if preview is not None:
            base_cd = preview.effective_proactive_cooldown
            if preview.opportunity.consecutive_unanswered_autonomy > 0:
                divisor = 1 << min(
                    preview.opportunity.consecutive_unanswered_autonomy,
                    preview.max_consecutive_unanswered - 1,
                )
                base_cd = preview.effective_proactive_cooldown / divisor
            backoff = "/".join(
                self._compact_duration(base_cd * (1 << index))
                for index in range(preview.max_consecutive_unanswered)
            )
            lines.extend(
                (
                    f"sleep_cap={preview.max_messages_per_sleep_session}",
                    (
                        "policy="
                        f"opportunity:{self._compact_duration(preview.opportunity_interval)} "
                        f"idle:{self._compact_duration(preview.min_user_idle)} "
                        f"base_cooldown:{self._compact_duration(base_cd)} "
                        f"max:{preview.max_messages_per_24h}/24h "
                        f"unanswered:{preview.max_consecutive_unanswered}"
                    ),
                    f"unanswered_backoff={backoff} then block-until-user",
                    (
                        f"signal_salience>={preview.min_signal_salience:.2f} "
                        f"planner_motivation>={preview.base_model_motivation_threshold:.2f}"
                    ),
                    f"idle_contact_drive={preview.idle_contact_drive:.2f}",
                    f"effective_raw_motivation_threshold={preview.raw_motivation_threshold:.3f}",
                )
            )
        config = self._spontaneity_composer.config if self._spontaneity_composer else None
        if config is not None:
            lines.extend(
                (
                    (
                        "spontaneity_policy="
                        f"delay:{config.min_delay.total_seconds():g}-"
                        f"{config.max_delay.total_seconds():g}s "
                        f"cooldown:{self._compact_duration(config.cooldown)} "
                        f"max:{config.max_messages_per_24h}/24h one-per-source-turn"
                    ),
                    (
                        "spontaneity_sampling="
                        f"direct:{config.direct_answer_sample_rate:.0%} "
                        f"short:{config.short_answer_sample_rate:.0%} "
                        f"interrupt-window:{config.interrupt_sample_rate:.0%} "
                        f"grace:{config.interrupt_grace_seconds:g}s"
                    ),
                )
            )

        generation = self._sessions.current_generation(chat_id)
        if self._autonomy_store is not None:
            last_user = self._sessions.last_user_message_at(chat_id)
            stats = self._autonomy_store.delivery_stats(
                chat_id,
                generation,
                now=now,
                last_user_message_at=last_user,
                sleep_started_at=(preferences.sleep_started_at if preferences.sleep_mode else None),
            )
            lines.extend(
                (
                    f"confirmed_last_24h={stats.autonomy_messages_last_24h}",
                    f"unanswered={stats.consecutive_unanswered_autonomy}",
                    f"sleep_deliveries={stats.sleep_messages_since_start}",
                    "last_delivery="
                    + (
                        stats.last_autonomy_message_at.isoformat()
                        if stats.last_autonomy_message_at is not None
                        else "none"
                    ),
                )
            )
            last_evaluation = self._autonomy_store.last_evaluation(chat_id, generation)
            if last_evaluation is not None:
                lines.extend(
                    (
                        f"last_eval_action={last_evaluation.action.value}",
                        f"last_eval_reason={last_evaluation.reason_label}",
                        f"last_eval_at={last_evaluation.evaluated_at.isoformat()}",
                    )
                )
        if self._spontaneity_store is not None:
            short_stats = self._spontaneity_store.delivery_stats(chat_id, generation, now=now)
            lines.extend(
                (
                    f"spontaneity_last_24h={short_stats.messages_last_24h}",
                    "spontaneity_last_delivery="
                    + (
                        short_stats.last_delivery_at.isoformat()
                        if short_stats.last_delivery_at is not None
                        else "none"
                    ),
                )
            )
        return "\n".join(lines)

    async def _preview(self, chat_id: int, *, now: datetime) -> AutonomyRuntimePreview | None:
        runtime = self._autonomy_runtime
        if runtime is None:
            return None
        preferences = self._preferences.autonomy_preferences(chat_id)
        try:
            return await runtime.preview(
                chat_id,
                at=now,
                do_not_disturb=preferences.do_not_disturb,
                sleep_mode=preferences.sleep_mode,
                sleep_started_at=(preferences.sleep_started_at if preferences.sleep_mode else None),
            )
        except ValueError:
            return None

    def _human_autonomy_lines(
        self,
        chat_id: int,
        preview: AutonomyRuntimePreview,
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        strongest = preview.strongest_signal
        topic_line = "  当前最想提起的话题：暂无"
        if strongest is not None:
            topic_line = (
                "  当前最想提起的话题："
                f"{self._strength_label(strongest.salience)} {strongest.salience:.0%} · "
                f"“{self._compact_text(strongest.summary, 48)}”"
            )
        opportunity = preview.opportunity
        last_eval_line = self._last_evaluation_line(chat_id, now=now)
        return (
            (
                "  想主动找你："
                f"{self._bar(preview.contact_urge)} {preview.contact_urge:.0%}"
                "（倾向，不是发送概率）"
            ),
            f"  沉默驱动力：{preview.idle_contact_drive:.0%}",
            topic_line,
            (
                f"  主动开口门槛：{preview.raw_motivation_threshold:.1%}"
                f"（基础 {preview.base_model_motivation_threshold:.0%}）"
            ),
            "  下一次主动思考：" + self._future_text(preview.next_opportunity_at, now=now),
            (
                f"  今日主动联系：{opportunity.autonomy_messages_last_24h} / "
                f"{preview.max_messages_per_24h}"
            ),
            (
                f"  连续未回复：{opportunity.consecutive_unanswered_autonomy} / "
                f"{preview.max_consecutive_unanswered}"
            ),
            "  当前为什么还没找你：" + self._blocker_text(preview, now=now),
            last_eval_line,
        )

    def _blocker_text(self, preview: AutonomyRuntimePreview, *, now: datetime) -> str:
        preferences = self._preferences.autonomy_preferences(preview.chat_id)
        if not self._autonomy_pilot_enabled:
            return "主动联系的运行时开关关闭。"
        if not preferences.enabled:
            return "你关闭了主动联系。"
        if self._conversation.has_active_turn(preview.chat_id):
            return "你们正在聊天，long-horizon 不会抢当前对话。"
        reason = preview.guard_reason_label
        opportunity = preview.opportunity
        if reason == "do_not_disturb":
            return "DND 开启。"
        if reason == "no_user_history":
            return "还没有足够的真实对话历史。"
        if reason == "daily_cap":
            return f"最近 24 小时已经达到 {preview.max_messages_per_24h} 条上限。"
        if reason == "unanswered_cap":
            return "连续主动找你多次都没收到回复，正在等你先说话。"
        if reason == "sleep_session_cap":
            return "睡眠模式本次允许的主动消息已经用完。"
        if reason == "user_recently_active" and opportunity.last_user_message_at is not None:
            idle = now - opportunity.last_user_message_at
            remaining = max(timedelta(0), preview.min_user_idle - idle)
            return "刚聊完不久；再过约 " + self._duration_text(remaining) + " 才进入长时主动窗口。"
        if reason == "proactive_cooldown" and opportunity.last_autonomy_message_at is not None:
            elapsed = now - opportunity.last_autonomy_message_at
            remaining = max(timedelta(0), preview.effective_proactive_cooldown - elapsed)
            return "上一条主动消息后的退避还剩约 " + self._duration_text(remaining) + "。"
        if reason == "no_salient_signal":
            return f"目前没有达到 {preview.min_signal_salience:.0%} 的真实话题/记忆信号。"
        if reason == "user_suppressed":
            return "当前被用户级抑制规则阻止。"
        if preview.next_opportunity_at > now:
            return "没有硬阻塞；等下一次主动思考，届时她仍可能选择沉默。"
        return "没有硬阻塞；主动思考已经到点，但她仍可能选择沉默。"

    def _last_evaluation_line(self, chat_id: int, *, now: datetime) -> str:
        if self._autonomy_store is None:
            return "  最近一次主动思考：暂无"
        evaluation = self._autonomy_store.last_evaluation(
            chat_id,
            self._sessions.current_generation(chat_id),
        )
        if evaluation is None:
            return "  最近一次主动思考：暂无"
        action = "选择沉默" if evaluation.action.value == "SILENT" else "准备主动开口"
        reason = self._reason_text(evaluation.reason_label)
        return (
            f"  最近一次主动思考：{action}（{reason}） · "
            f"{self._since_text(evaluation.evaluated_at, now=now)}前"
        )

    @staticmethod
    def _reason_text(reason: str) -> str:
        mapping = {
            "low_motivation": "当时动机不足",
            "guard_user_recently_active": "你刚刚还在说话",
            "guard_proactive_cooldown": "仍在退避",
            "guard_no_salient_signal": "没有足够强的话题",
            "guard_daily_cap": "达到 24 小时上限",
            "guard_unanswered_cap": "连续未回复上限",
            "guard_do_not_disturb": "DND",
            "autonomy_failure": "规划失败并安全沉默",
        }
        return mapping.get(reason, reason or "未标注原因")

    @staticmethod
    def _switch_text(chat_enabled: bool, runtime_enabled: bool) -> str:
        if chat_enabled and runtime_enabled:
            return "开启"
        if chat_enabled:
            return "已允许，但运行时 gate 关闭"
        return "关闭"

    @staticmethod
    def _bar(value: float) -> str:
        bounded = max(0.0, min(1.0, value))
        filled = int(round(bounded * 10))
        return "█" * filled + "░" * (10 - filled)

    @staticmethod
    def _strength_label(value: float) -> str:
        if value >= 0.80:
            return "很强"
        if value >= 0.65:
            return "较强"
        if value >= 0.50:
            return "中等"
        if value >= 0.35:
            return "偏弱"
        return "很弱"

    @staticmethod
    def _compact_text(text: str, limit: int) -> str:
        compact = " ".join(text.split())
        return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"

    @classmethod
    def _future_text(cls, at: datetime, *, now: datetime) -> str:
        remaining = at - now
        if remaining <= timedelta(seconds=1):
            return "现在 / 即将"
        return "约 " + cls._duration_text(remaining) + "后"

    @classmethod
    def _since_text(cls, at: datetime | None, *, now: datetime) -> str:
        if at is None:
            return "暂无"
        elapsed = max(timedelta(0), now - at)
        return cls._duration_text(elapsed)

    @staticmethod
    def _duration_text(value: timedelta) -> str:
        total_minutes = max(0, int(value.total_seconds() // 60))
        if total_minutes < 1:
            return "不到 1 分钟"
        if total_minutes < 60:
            return f"{total_minutes} 分钟"
        total_hours, minutes = divmod(total_minutes, 60)
        if total_hours < 24:
            return f"{total_hours} 小时" + (f" {minutes} 分钟" if minutes else "")
        days, hours = divmod(total_hours, 24)
        return f"{days} 天" + (f" {hours} 小时" if hours else "")

    @staticmethod
    def _compact_duration(value: timedelta) -> str:
        seconds = int(value.total_seconds())
        if seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        if seconds % 60 == 0:
            return f"{seconds // 60}m"
        return f"{seconds}s"

    @staticmethod
    def _short_usage_line(count: int, limit: int | None) -> str:
        if limit is None:
            return f"  最近 24h：{count} 条"
        return f"  最近 24h：{count} / {limit}"
