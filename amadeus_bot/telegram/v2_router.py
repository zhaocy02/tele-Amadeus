from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from amadeus_bot.character import (
    CharacterTurnTiming,
    ConversationAct,
    SpontaneityAction,
    SpontaneityComposer,
    SpontaneityOpportunity,
    SpontaneityOpportunityGate,
    SQLiteCharacterStateStore,
)
from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    MemoryStatus,
    StructuredMemoryRepository,
)
from amadeus_bot.memory.legacy_store import is_sensitive_memory
from amadeus_bot.runtime import (
    AutonomyRuntimeEvaluation,
    ConversationBusyError,
    ConversationCancelledError,
    ConversationSessionStore,
    SQLiteAutonomyRuntimeStore,
    SQLiteRuntimePreferenceStore,
    SQLiteSpontaneityStore,
    V2ConversationCoordinator,
)

from .adapter import IncomingMessage, TelegramGateway
from .v2_delivery import (
    V2TelegramAutonomyDeliveryResult,
    V2TelegramDeliveryAdapter,
    V2TelegramUserDeliveryResult,
)

LOGGER = logging.getLogger(__name__)

V2_HELP_TEXT = "\n".join(
    (
        "Amadeus v2 已连接。直接发消息就好。",
        "",
        "/memory [on|off] - 查看或开关长期记忆",
        "/remember <内容> - 明确记住一件事",
        "/forget <编号|文字> - 忘记匹配的长期记忆",
        "/profile - 查看保存的事实与偏好",
        "/history [轮数] - 查看最近对话",
        "/autonomy - 查看主动消息与短期续话状态",
        "/new - 开始新对话（长期记忆和角色状态保留）",
        "/status - 查看 v2 对话、记忆和最近延迟",
        "/cancel - 取消正在生成的回复",
        "/help - 显示帮助",
    )
)
_COMMAND_PATTERN = re.compile(r"^/([a-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", re.IGNORECASE)
_TELEGRAM_SAFE_TEXT_LIMIT = 3500
_MEMORY_PREVIEW_CHARS = 320
_SLEEP_SESSION_CAP = 2


@dataclass(frozen=True, slots=True)
class _LastTurnTiming:
    retrieval_ms: int
    character: CharacterTurnTiming
    time_to_send_ms: int
    finalize_ms: int


class V2TelegramMessageRouter:
    """User-facing v2 Telegram router with per-chat FIFO and bounded spontaneity races."""

    def __init__(
        self,
        *,
        gateway: TelegramGateway,
        delivery: V2TelegramDeliveryAdapter,
        conversation: V2ConversationCoordinator,
        memory: StructuredMemoryRepository,
        state: SQLiteCharacterStateStore,
        sessions: ConversationSessionStore,
        preferences: SQLiteRuntimePreferenceStore,
        autonomy_store: SQLiteAutonomyRuntimeStore | None = None,
        autonomy_pilot_enabled: bool = False,
        autonomy_timezone: str = "Asia/Shanghai",
        spontaneity_composer: SpontaneityComposer | None = None,
        spontaneity_gate: SpontaneityOpportunityGate | None = None,
        spontaneity_store: SQLiteSpontaneityStore | None = None,
        spontaneity_process_enabled: bool = False,
    ) -> None:
        self._gateway = gateway
        self._delivery = delivery
        self._conversation = conversation
        self._memory = memory
        self._state = state
        self._sessions = sessions
        self._preferences = preferences
        self._autonomy_store = autonomy_store
        self._autonomy_pilot_enabled = autonomy_pilot_enabled
        self._autonomy_timezone_name = autonomy_timezone
        self._spontaneity_composer = spontaneity_composer
        self._spontaneity_gate = spontaneity_gate
        self._spontaneity_store = spontaneity_store
        self._spontaneity_process_enabled = spontaneity_process_enabled
        self._last_turn_timing: dict[int, _LastTurnTiming] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._pending_user_handles: dict[int, int] = {}
        self._spontaneity_tasks: dict[int, asyncio.Task[None]] = {}
        self._spontaneity_committed: set[int] = set()
        self._spontaneity_interrupt_window: set[int] = set()

    async def aclose(self) -> None:
        tasks = tuple(self._spontaneity_tasks.items())
        for chat_id, task in tasks:
            if chat_id not in self._spontaneity_committed:
                task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        self._spontaneity_tasks.clear()
        self._spontaneity_committed.clear()
        self._spontaneity_interrupt_window.clear()

    async def handle(self, message: IncomingMessage) -> None:
        text = message.text.strip()
        command = self._parse_command(text)
        normalized_command = self._normalize_command(*command) if command is not None else None

        if normalized_command is not None:
            # Commands are explicit control traffic and never yield to a spontaneous continuation.
            self._cancel_spontaneity(message.chat_id)
        else:
            await self._arbitrate_spontaneity_before_user(message.chat_id)

        if normalized_command is not None and normalized_command[0] == "cancel":
            await self._handle_command(message, *normalized_command)
            return

        pending = self._pending_user_handles.get(message.chat_id, 0)
        self._pending_user_handles[message.chat_id] = pending + 1
        try:
            async with self._chat_lock(message.chat_id):
                if normalized_command is not None:
                    handled = await self._handle_command(message, *normalized_command)
                    if handled:
                        return
                await self._handle_chat(message)
        finally:
            remaining = self._pending_user_handles.get(message.chat_id, 1) - 1
            if remaining <= 0:
                self._pending_user_handles.pop(message.chat_id, None)
            else:
                self._pending_user_handles[message.chat_id] = remaining

    async def deliver_prepared_autonomy(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        text: str,
    ) -> V2TelegramAutonomyDeliveryResult:
        """Serialize final proactive sending while giving queued user input priority."""

        async with self._chat_lock(evaluation.chat_id):
            if self._pending_user_handles.get(evaluation.chat_id, 0) > 0:
                raise ValueError("queued user input takes priority over autonomy")
            preferences = self._preferences.autonomy_preferences(evaluation.chat_id)
            now = datetime.now(UTC)
            if not self._autonomy_pilot_enabled:
                raise ValueError("autonomy process gate disabled before delivery")
            if not preferences.enabled:
                raise ValueError("autonomy chat preference disabled before delivery")
            if preferences.do_not_disturb:
                raise ValueError("autonomy DND enabled before delivery")
            if preferences.sleep_mode:
                if preferences.sleep_started_at is None or self._autonomy_store is None:
                    raise ValueError("autonomy sleep state incomplete before delivery")
                stats = self._autonomy_store.delivery_stats(
                    evaluation.chat_id,
                    evaluation.generation,
                    now=now,
                    last_user_message_at=self._sessions.last_user_message_at(evaluation.chat_id),
                    sleep_started_at=preferences.sleep_started_at,
                )
                if stats.sleep_messages_since_start >= _SLEEP_SESSION_CAP:
                    raise ValueError("autonomy sleep-session cap reached before delivery")
            return await self._delivery.deliver_prepared_autonomy_message(evaluation, text)

    def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _handle_command(
        self,
        message: IncomingMessage,
        name: str,
        argument: str,
    ) -> bool:
        chat_id = message.chat_id
        if name in {"start", "help"}:
            await self._send_command_text(chat_id, V2_HELP_TEXT)
            return True
        if name == "status":
            await self._send_command_text(chat_id, await self._status_text(chat_id))
            return True
        if name == "new":
            try:
                self._conversation.start_new_conversation(chat_id)
            except ConversationBusyError:
                await self._send_command_text(
                    chat_id,
                    "当前回复仍在生成，请先 /cancel 或稍后再试。",
                )
            else:
                await self._send_command_text(
                    chat_id,
                    "好，我们从新的对话上下文继续。长期记忆和角色状态会保留。",
                )
            return True
        if name == "cancel":
            cancelled = await self._conversation.cancel(chat_id)
            await self._send_command_text(
                chat_id,
                "已请求停止这次回复。" if cancelled else "当前没有正在生成的回复。",
            )
            return True
        if name == "history":
            turns = self._parse_history_turns(argument)
            if turns is None:
                await self._send_command_text(
                    chat_id,
                    "用法：/history [轮数]，轮数范围为 1-20，默认 5。",
                )
            else:
                await self._send_command_text(chat_id, self._history_text(chat_id, turns))
            return True
        if name == "memory":
            await self._handle_memory(chat_id, argument)
            return True
        if name == "autonomy":
            await self._handle_autonomy(chat_id, argument)
            return True
        if name == "remember":
            await self._handle_remember(chat_id, argument)
            return True
        if name == "forget":
            await self._handle_forget(chat_id, argument)
            return True
        if name == "profile":
            records = await self._memory.list_active(
                kinds=(MemoryKind.FACT, MemoryKind.PREFERENCE),
                limit=20,
            )
            await self._send_command_text(
                chat_id,
                self._format_memories(records)
                if records
                else "目前没有保存事实或偏好。",
            )
            return True
        return False

    async def _handle_memory(self, chat_id: int, argument: str) -> None:
        normalized = argument.strip().lower()
        if normalized in {"on", "off"}:
            if self._conversation.has_active_turn(chat_id):
                await self._send_command_text(
                    chat_id,
                    "当前回复仍在生成，暂时不能切换长期记忆。",
                )
                return
            enabled = normalized == "on"
            previous = self._preferences.memory_enabled(chat_id)
            self._preferences.set_memory_enabled(chat_id, enabled)
            if previous != enabled:
                self._conversation.start_new_conversation(chat_id)
            await self._send_command_text(
                chat_id,
                (
                    "长期记忆已开启；相关记忆会用于之后的对话。"
                    if enabled
                    else "长期记忆已关闭；已有内容保留，但不会检索或自动新增。"
                ),
            )
            return
        if normalized:
            await self._send_command_text(chat_id, "用法：/memory、/memory on 或 /memory off")
            return
        records = await self._memory.list_active(limit=20)
        status = "开启" if self._preferences.memory_enabled(chat_id) else "关闭"
        body = self._format_memories(records) if records else "目前没有活动的长期记忆。"
        await self._send_command_text(chat_id, f"长期记忆：{status}\n\n{body}")

    async def _handle_autonomy(self, chat_id: int, argument: str) -> None:
        normalized = " ".join(argument.strip().lower().split())
        if normalized in {"", "status"}:
            await self._send_command_text(chat_id, self._autonomy_status_text(chat_id))
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
                text = (
                    "主动消息已开启：用户空闲至少 30 分钟，基础 cooldown 30 分钟；"
                    "未回复时 cooldown 动态退避，24 小时最多 12 条，连续 3 条未回复后暂停。"
                    "你说晚安/哦呀斯密后进入睡眠模式，直到说早上好，期间最多 2 条。"
                )
            else:
                text = "主动消息已关闭；long-horizon autonomy 与短期续话都会停止发送。"
            await self._send_command_text(chat_id, text)
            return
        if normalized in {"spontaneity on", "spontaneity off"}:
            enabled = normalized.endswith(" on")
            self._preferences.set_spontaneity_enabled(chat_id, enabled)
            preferences = self._preferences.autonomy_preferences(chat_id)
            if not enabled:
                text = "短期续话已关闭；普通回复后不会再延迟补发第二条。"
            elif not preferences.enabled:
                text = "已保存短期续话 opt-in；还需要 /autonomy on 才可能发送。"
            elif not self._spontaneity_process_enabled:
                text = "已保存短期续话 opt-in，但当前 spontaneity 进程级 gate 仍关闭。"
            else:
                text = (
                    "短期续话已开启：普通回复后约 6–50 秒内可能再说一条；"
                    "少量已经开始形成的续话，在你恰好发来普通消息时可能获得极短发送优先。"
                )
            await self._send_command_text(chat_id, text)
            return
        if normalized in {"dnd on", "dnd off"}:
            enabled = normalized.endswith(" on")
            self._preferences.set_autonomy_dnd(chat_id, enabled)
            await self._send_command_text(
                chat_id,
                "主动消息 DND 已开启。" if enabled else "主动消息 DND 已关闭。",
            )
            return
        if normalized.startswith("quiet"):
            await self._send_command_text(
                chat_id,
                "固定时间 quiet hours 已停用。现在使用语义睡眠模式：你说晚安/哦呀斯密后进入，"
                "说早上好后退出；睡眠期间 long-horizon 最多 2 条，短期续话暂停。"
                "DND 仍可用 /autonomy dnd on|off。",
            )
            return
        await self._send_command_text(
            chat_id,
            "用法：/autonomy、/autonomy on|off、/autonomy spontaneity on|off、"
            "/autonomy dnd on|off。睡眠模式由晚安/哦呀斯密与早上好自动切换。",
        )

    def _autonomy_status_text(self, chat_id: int) -> str:
        preferences = self._preferences.autonomy_preferences(chat_id)
        now = datetime.now(UTC)
        lines = [
            "Amadeus 主动消息",
            f"chat_opt_in={'on' if preferences.enabled else 'off'}",
            f"runtime_gate={'on' if self._autonomy_pilot_enabled else 'off'}",
            f"spontaneity_chat={'on' if preferences.spontaneity_enabled else 'off'}",
            f"spontaneity_runtime_gate={'on' if self._spontaneity_process_enabled else 'off'}",
            f"dnd={'on' if preferences.do_not_disturb else 'off'}",
            f"sleep_mode={'on' if preferences.sleep_mode else 'off'}",
            "sleep_trigger=晚安/哦呀斯密 -> 早上好",
            "sleep_cap=2",
            "policy=opportunity:15m idle:30m base_cooldown:30m max:12/24h unanswered:3",
            "unanswered_backoff=30m/60m/120m then block-until-user",
            "signal_salience>=0.50 planner_motivation>=0.50",
            "spontaneity_policy=delay:6-50s cooldown:2m max:18/24h one-per-source-turn",
            "spontaneity_sampling=direct:55% short:20% interrupt-window:12% grace:1.25s",
            "SILENT remains valid; bounded tangent/context grounding required",
        ]
        if preferences.sleep_started_at is not None:
            lines.append(f"sleep_started_at={preferences.sleep_started_at.isoformat()}")
        generation = self._sessions.current_generation(chat_id)
        if self._autonomy_store is not None:
            last_user = self._sessions.last_user_message_at(chat_id)
            stats = self._autonomy_store.delivery_stats(
                chat_id,
                generation,
                now=now,
                last_user_message_at=last_user,
                sleep_started_at=(
                    preferences.sleep_started_at if preferences.sleep_mode else None
                ),
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

    async def _handle_remember(self, chat_id: int, argument: str) -> None:
        content = argument.strip()
        if not content:
            await self._send_command_text(chat_id, "用法：/remember <希望我记住的内容>")
            return
        if len(content) > 600:
            await self._send_command_text(chat_id, "这条记忆太长了，请控制在 600 个字符以内。")
            return
        if is_sensitive_memory(content):
            await self._send_command_text(
                chat_id,
                "这条内容看起来包含凭据或其他敏感秘密，我不会把它写入长期记忆。",
            )
            return
        if self._conversation.has_active_turn(chat_id):
            await self._send_command_text(
                chat_id,
                "当前回复仍在生成，请稍后再修改长期记忆。",
            )
            return
        now = datetime.now(UTC)
        record = MemoryRecord(
            memory_id="mem_manual_" + uuid4().hex,
            kind=MemoryKind.FACT,
            content=content,
            confidence=1.0,
            salience=0.9,
            created_at=now,
            updated_at=now,
            source_message_ids=(),
            source_type=MemorySourceType.MANUAL,
            tags=("explicit_command",),
        )
        await self._memory.upsert(record)
        short_id = record.memory_id[-8:]
        await self._send_command_text(chat_id, f"记住了（ref {short_id}）。")

    async def _handle_forget(self, chat_id: int, argument: str) -> None:
        selector = argument.strip()
        records = await self._memory.list_active(limit=100)
        if not selector:
            body = self._format_memories(records[:20]) if records else "目前没有活动的长期记忆。"
            await self._send_command_text(chat_id, f"用法：/forget <编号|文字>\n\n{body}")
            return
        if self._conversation.has_active_turn(chat_id):
            await self._send_command_text(
                chat_id,
                "当前回复仍在生成，请稍后再修改长期记忆。",
            )
            return

        selected = self._select_memories(records, selector)
        if not selected:
            await self._send_command_text(chat_id, "没有找到匹配的活动记忆。")
            return
        now = datetime.now(UTC)
        changed = 0
        for record in selected:
            if await self._memory.set_status(
                record.memory_id,
                MemoryStatus.FORGOTTEN,
                at=now,
                detail="explicit_forget_command",
            ):
                changed += 1
        if changed:
            self._conversation.start_new_conversation(chat_id)
        await self._send_command_text(
            chat_id,
            f"已忘记 {changed} 条长期记忆。新的对话上下文已开始。",
        )

    async def _handle_chat(self, message: IncomingMessage) -> None:
        try:
            result = await self._delivery.deliver_user_message(message)
        except ConversationBusyError:
            await self._gateway.send_message(
                message.chat_id,
                "上一条回复还在生成。你可以等待，或发送 /cancel。",
            )
        except ConversationCancelledError:
            await self._gateway.send_message(message.chat_id, "这次回复已取消。")
        except Exception as exc:
            LOGGER.error("v2 Telegram turn failed: %s", type(exc).__name__)
            await self._gateway.send_message(message.chat_id, "这次回复失败了，请稍后再试。")
        else:
            timing = result.prepared.turn_result.timing
            self._last_turn_timing[message.chat_id] = _LastTurnTiming(
                retrieval_ms=result.prepared.retrieval_ms,
                character=timing,
                time_to_send_ms=result.time_to_send_ms,
                finalize_ms=result.finalize_ms,
            )
            LOGGER.info(
                "v2 turn timing policy_mode=%s retrieval_ms=%s policy_ms=%s context_ms=%s "
                "generation_ms=%s character_total_ms=%s time_to_send_ms=%s finalize_ms=%s",
                timing.policy_mode,
                result.prepared.retrieval_ms,
                timing.policy_ms,
                timing.context_ms,
                timing.generation_ms,
                timing.total_ms,
                result.time_to_send_ms,
                result.finalize_ms,
            )
            if result.warnings:
                LOGGER.warning("v2 delivered turn warnings: %s", ",".join(result.warnings))
            self._schedule_spontaneity(message.chat_id, result)

    def _schedule_spontaneity(
        self,
        chat_id: int,
        result: V2TelegramUserDeliveryResult,
    ) -> None:
        if not self._spontaneity_process_enabled:
            return
        composer = self._spontaneity_composer
        gate = self._spontaneity_gate
        store = self._spontaneity_store
        if composer is None or gate is None or store is None:
            return
        finalize = result.finalize
        if finalize is None or finalize.exchange is None:
            return
        preferences = self._preferences.autonomy_preferences(chat_id)
        if (
            not preferences.enabled
            or not preferences.spontaneity_enabled
            or preferences.do_not_disturb
            or preferences.sleep_mode
        ):
            return
        prepared = result.prepared
        policy_act = prepared.turn_result.policy.act
        if not gate.eligible(
            turn_id=prepared.turn_id,
            policy_act=policy_act,
            user_text=prepared.user_text,
            assistant_text=prepared.reply_text,
            user_images_count=prepared.user_images_count,
        ):
            return

        self._cancel_spontaneity(chat_id)
        generation = self._sessions.current_generation(chat_id)
        delay_seconds = gate.delay_for_turn(prepared.turn_id)
        task = asyncio.create_task(
            self._run_spontaneity(
                chat_id=chat_id,
                generation=generation,
                source_user_message_id=finalize.exchange.user_message_id,
                source_turn_id=prepared.turn_id,
                user_text=prepared.user_text,
                assistant_text=prepared.reply_text,
                policy_act=policy_act,
                delay_seconds=delay_seconds,
                allow_interrupt_window=gate.interrupt_window_allowed(prepared.turn_id),
            )
        )
        self._spontaneity_tasks[chat_id] = task

    async def _run_spontaneity(
        self,
        *,
        chat_id: int,
        generation: int,
        source_user_message_id: int,
        source_turn_id: str,
        user_text: str,
        assistant_text: str,
        policy_act: ConversationAct,
        delay_seconds: float,
        allow_interrupt_window: bool = False,
    ) -> None:
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(delay_seconds)
            if not self._spontaneity_preflight(
                chat_id,
                generation=generation,
                source_user_message_id=source_user_message_id,
            ):
                return

            composer = self._spontaneity_composer
            store = self._spontaneity_store
            if composer is None or store is None:
                return
            state = await self._state.load_state()
            now = datetime.now(UTC)
            state_summary = "" if state is None else state.render_prompt_context(at=now)
            opportunity = SpontaneityOpportunity(
                source_turn_id=source_turn_id,
                user_text=user_text,
                assistant_text=assistant_text,
                policy_act=policy_act,
                state_summary=state_summary,
                recent_conversation=self._sessions.history(chat_id, 8),
                created_at=now,
            )
            if allow_interrupt_window:
                self._spontaneity_interrupt_window.add(chat_id)
            decision = await composer.compose(opportunity)
            evaluation = store.record_evaluation(
                chat_id,
                generation,
                source_turn_id=source_turn_id,
                action=decision.action,
                reason_label=decision.reason_label,
                motivation=decision.motivation,
                delay_seconds=delay_seconds,
                at=datetime.now(UTC),
            )
            LOGGER.info(
                "spontaneity evaluation chat_id=%s source_turn=%s action=%s reason=%s "
                "delay=%.1fs interrupt_window=%s",
                chat_id,
                source_turn_id,
                decision.action.value,
                decision.reason_label or "unspecified",
                delay_seconds,
                str(allow_interrupt_window).lower(),
            )
            if decision.action is SpontaneityAction.SILENT:
                return
            if not self._spontaneity_preflight(
                chat_id,
                generation=generation,
                source_user_message_id=source_user_message_id,
            ):
                return

            async with self._chat_lock(chat_id):
                if not self._spontaneity_preflight(
                    chat_id,
                    generation=generation,
                    source_user_message_id=source_user_message_id,
                ):
                    return
                # This is the linearization point. A bounded interrupt window may let a nearly
                # completed continuation reach here just before a newly arrived ordinary message.
                # After commitment the Telegram send must finish so external success and local
                # persistence cannot diverge.
                self._spontaneity_committed.add(chat_id)
                delivery_result = await self._delivery.deliver_spontaneity_message(
                    chat_id=chat_id,
                    generation=generation,
                    source_turn_id=source_turn_id,
                    text=decision.text,
                    evaluation_id=evaluation.evaluation_id,
                )
                LOGGER.info(
                    "spontaneity delivered chat_id=%s source_turn=%s telegram_message_id=%s "
                    "warnings=%s",
                    chat_id,
                    source_turn_id,
                    delivery_result.telegram_message_id,
                    len(delivery_result.warnings),
                )
                if delivery_result.warnings:
                    LOGGER.warning(
                        "spontaneity delivered warnings: %s",
                        ",".join(delivery_result.warnings),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "spontaneity task failed chat_id=%s source_turn=%s type=%s",
                chat_id,
                source_turn_id,
                type(exc).__name__,
            )
        finally:
            self._spontaneity_interrupt_window.discard(chat_id)
            self._spontaneity_committed.discard(chat_id)
            if current_task is not None and self._spontaneity_tasks.get(chat_id) is current_task:
                self._spontaneity_tasks.pop(chat_id, None)

    def _spontaneity_preflight(
        self,
        chat_id: int,
        *,
        generation: int,
        source_user_message_id: int,
    ) -> bool:
        composer = self._spontaneity_composer
        store = self._spontaneity_store
        if not self._spontaneity_process_enabled or composer is None or store is None:
            return False
        if self._pending_user_handles.get(chat_id, 0) > 0:
            return False
        if self._conversation.has_active_turn(chat_id):
            return False
        if self._sessions.current_generation(chat_id) != generation:
            return False
        if self._sessions.last_user_message_id(chat_id) != source_user_message_id:
            return False
        preferences = self._preferences.autonomy_preferences(chat_id)
        if (
            not preferences.enabled
            or not preferences.spontaneity_enabled
            or preferences.do_not_disturb
            or preferences.sleep_mode
        ):
            return False
        now = datetime.now(UTC)
        stats = store.delivery_stats(chat_id, generation, now=now)
        config = composer.config
        if stats.messages_last_24h >= config.max_messages_per_24h:
            return False
        return not (
            stats.last_delivery_at is not None
            and now - stats.last_delivery_at < config.cooldown
        )

    async def _arbitrate_spontaneity_before_user(self, chat_id: int) -> None:
        if chat_id in self._spontaneity_committed:
            return
        task = self._spontaneity_tasks.get(chat_id)
        composer = self._spontaneity_composer
        if task is None or task.done():
            return
        if chat_id not in self._spontaneity_interrupt_window or composer is None:
            self._cancel_spontaneity(chat_id)
            return

        grace = composer.config.interrupt_grace_seconds
        if grace <= 0:
            self._cancel_spontaneity(chat_id)
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace)
        except TimeoutError:
            self._cancel_spontaneity(chat_id)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        else:
            LOGGER.info(
                "spontaneity interrupt window completed before inbound user handling chat_id=%s",
                chat_id,
            )

    def _cancel_spontaneity(self, chat_id: int) -> None:
        if chat_id in self._spontaneity_committed:
            return
        self._spontaneity_interrupt_window.discard(chat_id)
        task = self._spontaneity_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _status_text(self, chat_id: int) -> str:
        state = await self._state.load_state()
        active_memories = await self._memory.list_active(limit=500)
        generation_active = "yes" if self._conversation.has_active_turn(chat_id) else "no"
        autonomy_preferences = self._preferences.autonomy_preferences(chat_id)
        interrupt_window_active = chat_id in self._spontaneity_interrupt_window
        lines = [
            "Amadeus v2 状态",
            f"conversation_generation={self._sessions.current_generation(chat_id)}",
            f"delivered_turns={self._sessions.v2_turn_count(chat_id)}",
            f"memory={'on' if self._preferences.memory_enabled(chat_id) else 'off'}",
            f"active_memories={len(active_memories)}",
            f"character_state_version={state.state_version if state is not None else 0}",
            f"generation_active={generation_active}",
            f"autonomy={'on' if autonomy_preferences.enabled else 'off'}",
            f"autonomy_runtime_gate={'on' if self._autonomy_pilot_enabled else 'off'}",
            f"autonomy_sleep_mode={'on' if autonomy_preferences.sleep_mode else 'off'}",
            f"spontaneity={'on' if autonomy_preferences.spontaneity_enabled else 'off'}",
            f"spontaneity_runtime_gate={'on' if self._spontaneity_process_enabled else 'off'}",
            f"spontaneity_pending={'yes' if chat_id in self._spontaneity_tasks else 'no'}",
            f"spontaneity_interrupt_window={'yes' if interrupt_window_active else 'no'}",
        ]
        last = self._last_turn_timing.get(chat_id)
        if last is not None:
            timing = last.character
            lines.extend(
                (
                    f"last_policy_mode={timing.policy_mode}",
                    f"last_retrieval_ms={last.retrieval_ms}",
                    f"last_policy_ms={timing.policy_ms}",
                    f"last_generation_ms={timing.generation_ms}",
                    f"last_character_total_ms={timing.total_ms}",
                    f"last_time_to_send_ms={last.time_to_send_ms}",
                    f"last_finalize_ms={last.finalize_ms}",
                )
            )
        return "\n".join(lines)

    def _history_text(self, chat_id: int, turns: int) -> str:
        records = self._sessions.history_records(chat_id, turns * 2)
        if not records:
            return "当前对话还没有已送达的历史记录。"
        lines = [f"最近对话（最多 {turns} 轮）："]
        for record in records:
            role = "你" if record.role.value == "user" else "Amadeus"
            lines.append(f"{role}: {record.content}")
        return "\n".join(lines)

    async def _send_command_text(self, chat_id: int, text: str) -> None:
        chunks = self._split_telegram_text(text)
        try:
            for chunk in chunks:
                await self._gateway.send_message(chat_id, chunk)
        except Exception as exc:
            LOGGER.warning("v2 command response send failed: %s", type(exc).__name__)
            await self._gateway.send_message(chat_id, "命令结果发送失败，请稍后重试。")

    @staticmethod
    def _split_telegram_text(text: str) -> tuple[str, ...]:
        value = text.strip()
        if not value:
            return ("（空）",)
        chunks: list[str] = []
        remaining = value
        while len(remaining) > _TELEGRAM_SAFE_TEXT_LIMIT:
            cut = remaining.rfind("\n", 0, _TELEGRAM_SAFE_TEXT_LIMIT + 1)
            if cut < _TELEGRAM_SAFE_TEXT_LIMIT // 2:
                cut = remaining.rfind(" ", 0, _TELEGRAM_SAFE_TEXT_LIMIT + 1)
            if cut < _TELEGRAM_SAFE_TEXT_LIMIT // 2:
                cut = _TELEGRAM_SAFE_TEXT_LIMIT
            chunk = remaining[:cut].rstrip()
            if not chunk:
                chunk = remaining[:_TELEGRAM_SAFE_TEXT_LIMIT]
                cut = len(chunk)
            chunks.append(chunk)
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        return tuple(chunks)

    @staticmethod
    def _format_memories(records: tuple[MemoryRecord, ...]) -> str:
        if not records:
            return "目前没有活动的长期记忆。"
        return "\n".join(
            V2TelegramMessageRouter._format_memory_summary(index, record)
            for index, record in enumerate(records, start=1)
        )

    @staticmethod
    def _format_memory_summary(index: int, record: MemoryRecord) -> str:
        compact = " ".join(record.content.split())
        if len(compact) > _MEMORY_PREVIEW_CHARS:
            compact = compact[: _MEMORY_PREVIEW_CHARS - 1].rstrip() + "…"
        short_id = record.memory_id[-8:]
        date = record.created_at.astimezone(UTC).strftime("%Y-%m-%d")
        return f"{index}. [{record.kind.value}] {compact}\n   {date} · ref {short_id}"

    @staticmethod
    def _select_memories(
        records: tuple[MemoryRecord, ...],
        selector: str,
    ) -> tuple[MemoryRecord, ...]:
        try:
            ordinal = int(selector)
        except ValueError:
            ordinal = 0
        if 1 <= ordinal <= len(records):
            return (records[ordinal - 1],)
        normalized = selector.casefold()
        return tuple(
            record
            for record in records
            if normalized in record.content.casefold()
            or record.memory_id.casefold() == normalized
            or record.memory_id.casefold().endswith(normalized)
        )

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str] | None:
        match = _COMMAND_PATTERN.match(text)
        if match is None:
            return None
        return match.group(1).lower(), (match.group(2) or "").strip()

    @staticmethod
    def _normalize_command(name: str, argument: str) -> tuple[str, str]:
        if not argument and name.startswith("history"):
            suffix = name[len("history") :]
            if suffix.isdigit():
                return "history", suffix
        return name, argument

    @staticmethod
    def _parse_history_turns(argument: str) -> int | None:
        if not argument.strip():
            return 5
        try:
            value = int(argument.strip())
        except ValueError:
            return None
        return value if 1 <= value <= 20 else None
