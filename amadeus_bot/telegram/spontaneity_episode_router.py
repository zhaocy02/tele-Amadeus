from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from amadeus_bot.character import ConversationAct, SpontaneityAction, SpontaneityOpportunity

from .provider_router import V2TelegramMessageRouter as _ProviderRouter
from .v2_delivery import V2TelegramUserDeliveryResult

LOGGER = logging.getLogger(__name__)


class V2TelegramMessageRouter(_ProviderRouter):
    """Provider-aware router with bounded multi-message short-horizon thought episodes."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._episode_first_resolved: dict[int, asyncio.Event] = {}

    def _schedule_spontaneity(
        self,
        chat_id: int,
        result: V2TelegramUserDeliveryResult,
    ) -> None:
        control = self._provider_control
        if control is None:
            self._schedule_spontaneity_episode(chat_id, result)
            return
        # create_task captures ContextVars, so the whole delayed episode keeps the provider
        # selection from the source chat even after this short context manager exits.
        with control.use_chat(chat_id):
            self._schedule_spontaneity_episode(chat_id, result)

    def _schedule_spontaneity_episode(
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
        first_resolved = asyncio.Event()
        self._episode_first_resolved[chat_id] = first_resolved
        task = asyncio.create_task(
            self._run_spontaneity_episode(
                chat_id=chat_id,
                generation=generation,
                source_user_message_id=finalize.exchange.user_message_id,
                source_turn_id=prepared.turn_id,
                user_text=prepared.user_text,
                assistant_text=prepared.reply_text,
                policy_act=policy_act,
                allow_first_interrupt_window=gate.interrupt_window_allowed(prepared.turn_id),
                first_resolved=first_resolved,
            )
        )
        self._spontaneity_tasks[chat_id] = task

    async def _run_spontaneity_episode(
        self,
        *,
        chat_id: int,
        generation: int,
        source_user_message_id: int,
        source_turn_id: str,
        user_text: str,
        assistant_text: str,
        policy_act: ConversationAct,
        allow_first_interrupt_window: bool,
        first_resolved: asyncio.Event | None = None,
    ) -> None:
        current_task = asyncio.current_task()
        composer = self._spontaneity_composer
        gate = self._spontaneity_gate
        store = self._spontaneity_store
        if composer is None or gate is None or store is None:
            if first_resolved is not None:
                first_resolved.set()
            return

        latest_assistant_text = assistant_text
        try:
            for sequence_index in range(1, composer.config.max_followups_per_episode + 1):
                if not gate.followup_allowed(source_turn_id, sequence_index):
                    LOGGER.info(
                        "spontaneity episode stopped by depth gate chat_id=%s source_turn=%s "
                        "sequence=%s",
                        chat_id,
                        source_turn_id,
                        sequence_index,
                    )
                    return

                delay_seconds = gate.delay_for_followup(source_turn_id, sequence_index)
                deadline = monotonic() + delay_seconds

                # Start forming the thought early so provider latency overlaps the human-like pause.
                await asyncio.sleep(min(1.0, delay_seconds))
                if not self._episode_preflight(
                    chat_id,
                    generation=generation,
                    source_user_message_id=source_user_message_id,
                    enforce_episode_cooldown=sequence_index == 1,
                ):
                    return

                state = await self._state.load_state()
                now = datetime.now(UTC)
                state_summary = "" if state is None else state.render_prompt_context(at=now)
                opportunity = SpontaneityOpportunity(
                    source_turn_id=source_turn_id,
                    user_text=user_text,
                    assistant_text=latest_assistant_text,
                    policy_act=policy_act,
                    state_summary=state_summary,
                    recent_conversation=self._sessions.history(chat_id, 8),
                    created_at=now,
                    sequence_index=sequence_index,
                )

                first_interrupt = sequence_index == 1 and allow_first_interrupt_window
                if first_interrupt:
                    self._spontaneity_interrupt_window.add(chat_id)
                try:
                    decision = await composer.compose(opportunity)
                finally:
                    if first_interrupt:
                        self._spontaneity_interrupt_window.discard(chat_id)

                remaining = deadline - monotonic()
                if remaining > 0:
                    await asyncio.sleep(remaining)

                delivery_source_turn_id = self._episode_message_key(
                    source_turn_id,
                    sequence_index,
                )
                evaluation = store.record_evaluation(
                    chat_id,
                    generation,
                    source_turn_id=delivery_source_turn_id,
                    action=decision.action,
                    reason_label=self._episode_reason_label(decision.reason_label, sequence_index),
                    motivation=decision.motivation,
                    delay_seconds=delay_seconds,
                    at=datetime.now(UTC),
                )
                LOGGER.info(
                    "spontaneity episode evaluation chat_id=%s source_turn=%s sequence=%s "
                    "action=%s reason=%s delay=%.1fs interrupt_window=%s",
                    chat_id,
                    source_turn_id,
                    sequence_index,
                    decision.action.value,
                    decision.reason_label or "unspecified",
                    delay_seconds,
                    str(first_interrupt).lower(),
                )
                if decision.action is SpontaneityAction.SILENT:
                    return
                if not self._episode_preflight(
                    chat_id,
                    generation=generation,
                    source_user_message_id=source_user_message_id,
                    enforce_episode_cooldown=sequence_index == 1,
                ):
                    return

                async with self._chat_lock(chat_id):
                    if not self._episode_preflight(
                        chat_id,
                        generation=generation,
                        source_user_message_id=source_user_message_id,
                        enforce_episode_cooldown=sequence_index == 1,
                    ):
                        return
                    self._spontaneity_committed.add(chat_id)
                    try:
                        delivery_result = await self._delivery.deliver_spontaneity_message(
                            chat_id=chat_id,
                            generation=generation,
                            source_turn_id=delivery_source_turn_id,
                            text=decision.text,
                            evaluation_id=evaluation.evaluation_id,
                        )
                    finally:
                        self._spontaneity_committed.discard(chat_id)

                delivery_id = (
                    None
                    if delivery_result.stored_delivery is None
                    else delivery_result.stored_delivery.delivery_id
                )
                store.record_episode_message(
                    chat_id,
                    generation,
                    episode_id=source_turn_id,
                    source_turn_id=source_turn_id,
                    sequence_index=sequence_index,
                    delivery_source_turn_id=delivery_source_turn_id,
                    evaluation_id=evaluation.evaluation_id,
                    delivery_id=delivery_id,
                    at=datetime.now(UTC),
                )
                latest_assistant_text = decision.text
                if sequence_index == 1 and first_resolved is not None:
                    first_resolved.set()
                LOGGER.info(
                    "spontaneity episode delivered chat_id=%s source_turn=%s sequence=%s "
                    "telegram_message_id=%s warnings=%s",
                    chat_id,
                    source_turn_id,
                    sequence_index,
                    delivery_result.telegram_message_id,
                    len(delivery_result.warnings),
                )
                if delivery_result.warnings:
                    LOGGER.warning(
                        "spontaneity episode delivered warnings: %s",
                        ",".join(delivery_result.warnings),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.warning(
                "spontaneity episode failed chat_id=%s source_turn=%s type=%s",
                chat_id,
                source_turn_id,
                type(exc).__name__,
            )
        finally:
            if first_resolved is not None:
                first_resolved.set()
            self._spontaneity_interrupt_window.discard(chat_id)
            self._spontaneity_committed.discard(chat_id)
            if current_task is not None and self._spontaneity_tasks.get(chat_id) is current_task:
                self._spontaneity_tasks.pop(chat_id, None)
            if self._episode_first_resolved.get(chat_id) is first_resolved:
                self._episode_first_resolved.pop(chat_id, None)

    async def _arbitrate_spontaneity_before_user(self, chat_id: int) -> None:
        task = self._spontaneity_tasks.get(chat_id)
        if task is None or task.done():
            return

        first_resolved = self._episode_first_resolved.get(chat_id)
        if first_resolved is None:
            await super()._arbitrate_spontaneity_before_user(chat_id)
            return

        if chat_id in self._spontaneity_committed:
            return
        composer = self._spontaneity_composer
        if chat_id not in self._spontaneity_interrupt_window or composer is None:
            self._cancel_spontaneity(chat_id)
            return

        grace = composer.config.interrupt_grace_seconds
        if grace <= 0:
            self._cancel_spontaneity(chat_id)
            return
        try:
            await asyncio.wait_for(first_resolved.wait(), timeout=grace)
        except TimeoutError:
            self._cancel_spontaneity(chat_id)
            return

        # If the first thought won the tiny interrupt race, user input proceeds immediately and
        # terminates any remaining episode depth. This keeps interruption bounded to one utterance.
        if not task.done():
            self._cancel_spontaneity(chat_id)
        LOGGER.info(
            "spontaneity first-message interrupt resolved before inbound user handling chat_id=%s",
            chat_id,
        )

    def _episode_preflight(
        self,
        chat_id: int,
        *,
        generation: int,
        source_user_message_id: int,
        enforce_episode_cooldown: bool,
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
        tuning = self._autonomy_tuning
        if tuning is not None:
            hot_cap = tuning.get(chat_id).spontaneity_max_messages_per_24h
            if hot_cap is not None and stats.messages_last_24h >= hot_cap:
                return False
        if not enforce_episode_cooldown:
            return True
        return not (
            stats.last_delivery_at is not None
            and now - stats.last_delivery_at < config.cooldown
        )

    @staticmethod
    def _episode_message_key(source_turn_id: str, sequence_index: int) -> str:
        if sequence_index == 1:
            return source_turn_id
        return f"{source_turn_id}::spont:{sequence_index}"

    @staticmethod
    def _episode_reason_label(reason_label: str, sequence_index: int) -> str:
        reason = reason_label.strip() or "continued_thought"
        return f"episode_{sequence_index}:{reason}"[:120]

    def _autonomy_status_text(self, chat_id: int) -> str:
        return self._rewrite_spontaneity_status(super()._autonomy_status_text(chat_id))

    async def _autonomy_live_status_text(self, chat_id: int) -> str:
        return self._rewrite_spontaneity_status(
            await super()._autonomy_live_status_text(chat_id)
        )

    async def _debug_status_text(self, chat_id: int) -> str:
        return self._rewrite_spontaneity_status(await super()._debug_status_text(chat_id))

    def _rewrite_spontaneity_status(self, text: str) -> str:
        composer = self._spontaneity_composer
        if composer is None:
            return text
        config = composer.config
        first = (
            f"{int(config.min_delay.total_seconds())}-"
            f"{int(config.max_delay.total_seconds())}s"
        )
        chain = (
            f"{int(config.chain_min_delay.total_seconds())}-"
            f"{int(config.chain_max_delay.total_seconds())}s"
        )
        text = text.replace(
            f"spontaneity_policy=delay:{first} ",
            f"spontaneity_policy=first:{first} chain:{chain} ",
        )
        text = text.replace(
            f"spontaneity_live_policy=delay:{first} ",
            f"spontaneity_live_policy=first:{first} chain:{chain} ",
        )
        text = text.replace(
            " one-per-source-turn",
            f" episode-cap:{config.max_followups_per_episode}",
        )
        return text

    async def _handle_autonomy(self, chat_id: int, argument: str) -> None:
        normalized = " ".join(argument.strip().lower().split())
        if normalized in {"spontaneity on", "spontaneity off"}:
            enabled = normalized.endswith(" on")
            self._preferences.set_spontaneity_enabled(chat_id, enabled)
            preferences = self._preferences.autonomy_preferences(chat_id)
            if not enabled:
                text = "短期 spontaneous follow-up 已关闭。"
            elif not preferences.enabled:
                text = "已保存短期续话 opt-in；还需要 /autonomy on 才可能发送。"
            elif not self._spontaneity_process_enabled:
                text = "已保存短期续话 opt-in，但当前 spontaneity 进程级 gate 仍关闭。"
            else:
                config = self._spontaneity_composer.config if self._spontaneity_composer else None
                cap = 7 if config is None else config.max_followups_per_episode
                text = (
                    "短期续话已开启：普通回复后约 1–30 秒内可能开始一段短思绪；"
                    f"同一 episode 最多 {cap} 条，每一条都可选择停止，越往后越难继续。"
                )
            await self._send_command_text(chat_id, text)
            return
        await super()._handle_autonomy(chat_id, argument)
