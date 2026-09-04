import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.character import SQLiteCharacterStateStore
from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    StructuredMemoryRepository,
)
from amadeus_bot.runtime import ConversationSessionStore, SQLiteRuntimePreferenceStore
from amadeus_bot.telegram import IncomingMessage, V2TelegramMessageRouter


class FakeGateway:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> int:
        if len(text) > 4096:
            raise RuntimeError("Telegram message too long")
        self.messages.append((chat_id, text))
        return len(self.messages)

    async def send_typing(self, chat_id: int) -> None:
        return None


class FakeConversation:
    def __init__(self, sessions: ConversationSessionStore) -> None:
        self.sessions = sessions
        self.active = False
        self.cancelled = False

    def has_active_turn(self, chat_id: int) -> bool:
        return self.active

    def start_new_conversation(self, chat_id: int) -> None:
        self.sessions.rotate(chat_id)

    async def cancel(self, chat_id: int) -> bool:
        if not self.active:
            return False
        self.active = False
        self.cancelled = True
        return True


class FakeDelivery:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []

    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        self.messages.append(message)
        return _delivery_result()


class BlockingDelivery:
    def __init__(self, conversation: FakeConversation | None = None) -> None:
        self.messages: list[IncomingMessage] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._conversation = conversation

    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        self.messages.append(message)
        if self._conversation is not None:
            self._conversation.active = True
        self.started.set()
        await self.release.wait()
        if self._conversation is not None:
            self._conversation.active = False
        return _delivery_result()


class ConcurrentDelivery:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []
        self.active = 0
        self.max_active = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def deliver_user_message(self, message: IncomingMessage) -> SimpleNamespace:
        self.messages.append(message)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.active >= 2:
            self.both_started.set()
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return _delivery_result()


def _delivery_result() -> SimpleNamespace:
    timing = SimpleNamespace(
        policy_ms=2,
        context_ms=1,
        generation_ms=800,
        total_ms=803,
        policy_mode="fast",
    )
    prepared = SimpleNamespace(retrieval_ms=3, turn_result=SimpleNamespace(timing=timing))
    return SimpleNamespace(
        warnings=(),
        prepared=prepared,
        time_to_send_ms=850,
        finalize_ms=40,
    )


def _message(text: str, message_id: int = 1, *, chat_id: int = 10) -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=chat_id,
        message_id=message_id,
        text=text,
        received_at=datetime.now(UTC),
    )


def _build_router(tmp_path: Path) -> tuple[
    FakeGateway,
    StructuredMemoryRepository,
    SQLiteCharacterStateStore,
    ConversationSessionStore,
    SQLiteRuntimePreferenceStore,
    FakeConversation,
    FakeDelivery,
    V2TelegramMessageRouter,
]:
    gateway = FakeGateway()
    memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
    state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
    conversation = FakeConversation(sessions)
    delivery = FakeDelivery()
    router = V2TelegramMessageRouter(
        gateway=gateway,
        delivery=cast(Any, delivery),
        conversation=cast(Any, conversation),
        memory=memory,
        state=state,
        sessions=sessions,
        preferences=preferences,
    )
    return gateway, memory, state, sessions, preferences, conversation, delivery, router


def test_v2_router_memory_commands_rotate_context_and_keep_explicit_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway, memory, state, sessions, preferences, _, _, router = _build_router(tmp_path)
        try:
            assert sessions.current_generation(10) == 1
            await router.handle(_message("/remember 我喜欢咖啡"))
            assert "记住了" in gateway.messages[-1][1]

            await router.handle(_message("/memory"))
            assert "我喜欢咖啡" in gateway.messages[-1][1]
            assert "长期记忆：开启" in gateway.messages[-1][1]

            await router.handle(_message("/memory off"))
            assert preferences.memory_enabled(10) is False
            assert sessions.current_generation(10) == 2
            assert "长期记忆已关闭" in gateway.messages[-1][1]

            await router.handle(_message("/memory"))
            assert "我喜欢咖啡" in gateway.messages[-1][1]
            assert "长期记忆：关闭" in gateway.messages[-1][1]

            await router.handle(_message("/forget 1"))
            assert sessions.current_generation(10) == 3
            assert "已忘记 1 条长期记忆" in gateway.messages[-1][1]
            assert await memory.list_active(limit=20) == ()
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())


def test_v2_router_rejects_sensitive_manual_memory_and_routes_plain_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway, memory, state, sessions, preferences, _, delivery, router = _build_router(tmp_path)
        try:
            await router.handle(_message("/remember password=super-secret"))
            assert "不会把它写入长期记忆" in gateway.messages[-1][1]
            assert await memory.list_active(limit=20) == ()

            await router.handle(_message("你好", message_id=2))
            assert [message.text for message in delivery.messages] == ["你好"]

            await router.handle(_message("/status", message_id=3))
            status = gateway.messages[-1][1]
            assert "last_policy_mode=fast" in status
            assert "last_retrieval_ms=3" in status
            assert "last_policy_ms=2" in status
            assert "last_generation_ms=800" in status
            assert "last_time_to_send_ms=850" in status
            assert "last_finalize_ms=40" in status
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())


def test_v2_router_serializes_plain_messages_within_one_chat(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway, memory, state, sessions, preferences, conversation, _, router = _build_router(
            tmp_path
        )
        delivery = BlockingDelivery()
        router._delivery = cast(Any, delivery)
        try:
            first = asyncio.create_task(router.handle(_message("第一条", message_id=1)))
            await asyncio.wait_for(delivery.started.wait(), timeout=1)
            second = asyncio.create_task(router.handle(_message("第二条", message_id=2)))
            await asyncio.sleep(0)

            assert [message.text for message in delivery.messages] == ["第一条"]
            assert not second.done()

            delivery.release.set()
            await asyncio.gather(first, second)
            assert [message.text for message in delivery.messages] == ["第一条", "第二条"]
            assert gateway.messages == []
            assert conversation.cancelled is False
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())


def test_v2_router_keeps_different_chats_concurrent(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway, memory, state, sessions, preferences, _, _, router = _build_router(tmp_path)
        delivery = ConcurrentDelivery()
        router._delivery = cast(Any, delivery)
        try:
            first = asyncio.create_task(router.handle(_message("chat-10", chat_id=10)))
            second = asyncio.create_task(router.handle(_message("chat-20", chat_id=20)))
            await asyncio.wait_for(delivery.both_started.wait(), timeout=1)

            assert delivery.max_active == 2
            assert {message.chat_id for message in delivery.messages} == {10, 20}

            delivery.release.set()
            await asyncio.gather(first, second)
            assert gateway.messages == []
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())


def test_v2_router_cancel_bypasses_same_chat_fifo_lock(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway, memory, state, sessions, preferences, conversation, _, router = _build_router(
            tmp_path
        )
        delivery = BlockingDelivery(conversation)
        router._delivery = cast(Any, delivery)
        try:
            active_turn = asyncio.create_task(router.handle(_message("还在生成", message_id=1)))
            await asyncio.wait_for(delivery.started.wait(), timeout=1)
            assert conversation.active is True

            await asyncio.wait_for(router.handle(_message("/cancel", message_id=2)), timeout=1)
            assert conversation.cancelled is True
            assert "已请求停止这次回复" in gateway.messages[-1][1]
            assert not active_turn.done()

            delivery.release.set()
            await active_turn
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())


def test_v2_router_memory_summarizes_large_continuity_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway, memory, state, sessions, preferences, _, _, router = _build_router(tmp_path)
        try:
            now = datetime.now(UTC)
            for index in range(5):
                await memory.upsert(
                    MemoryRecord(
                        memory_id=f"legacy_episode_{index}",
                        kind=MemoryKind.EPISODE,
                        content=(f"continuity-{index} " + "很长的历史内容" * 500),
                        confidence=1.0,
                        salience=0.96,
                        created_at=now,
                        updated_at=now,
                        source_message_ids=(),
                        source_type=MemorySourceType.MIGRATED,
                    )
                )

            await router.handle(_message("/memory"))

            combined = "\n".join(text for _, text in gateway.messages)
            assert "长期记忆：开启" in combined
            assert "continuity-0" in combined
            assert "…" in combined
            assert all(len(text) <= 3500 for _, text in gateway.messages)
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())


def test_v2_router_history20_alias_chunks_large_history_without_llm(tmp_path: Path) -> None:
    async def scenario() -> None:
        gateway, memory, state, sessions, preferences, _, delivery, router = _build_router(tmp_path)
        try:
            for index in range(20):
                sessions.append_exchange(
                    10,
                    f"user-{index} " + "问题" * 120,
                    f"assistant-{index} " + "回答" * 120,
                )

            await router.handle(_message("/history20"))

            assert delivery.messages == []
            assert len(gateway.messages) > 1
            assert "最近对话（最多 20 轮）" in gateway.messages[0][1]
            combined = "\n".join(text for _, text in gateway.messages)
            assert "user-0" in combined
            assert "assistant-19" in combined
            assert all(len(text) <= 3500 for _, text in gateway.messages)
        finally:
            memory.close()
            state.close()
            sessions.close()
            preferences.close()

    asyncio.run(scenario())
