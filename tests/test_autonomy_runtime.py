import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from amadeus_bot.character import (
    AutonomyAction,
    AutonomyOpportunityScheduler,
    AutonomyPlanner,
    SQLiteCharacterStateStore,
)
from amadeus_bot.llm import LLMRequest, LLMResponse
from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    StructuredMemoryRepository,
)
from amadeus_bot.runtime import (
    AutonomyRuntimeCoordinator,
    ConversationSessionStore,
    SQLiteAutonomyRuntimeStore,
)


class AutonomyProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        assert request.metadata.get("prompt_version") == "autonomy-policy-v3-stronger-idle-drive"
        return LLMResponse(
            text=(
                '{"action":"FOLLOW_UP","selected_signal_id":"open-thread:thread_1",'
                '"motivation":0.82,"focus":"继续之前没聊完的话题",'
                '"reason_label":"grounded_follow_up"}'
            ),
            model="fake-autonomy",
        )


class RuntimeFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.provider = AutonomyProvider()
        self.memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        self.state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        self.sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        self.autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
        self.runtime = AutonomyRuntimeCoordinator(
            planner=AutonomyPlanner(provider=self.provider),
            scheduler=AutonomyOpportunityScheduler(),
            memory_repository=self.memory,
            state_store=self.state,
            sessions=self.sessions,
            autonomy_store=self.autonomy_store,
        )

    def close(self) -> None:
        self.memory.close()
        self.state.close()
        self.sessions.close()
        self.autonomy_store.close()


async def _seed_open_thread(runtime: RuntimeFixture, *, at: datetime) -> None:
    await runtime.memory.upsert(
        MemoryRecord(
            memory_id="thread_1",
            kind=MemoryKind.OPEN_THREAD,
            content="用户之前提到想继续讨论角色记忆边界。",
            confidence=0.95,
            salience=0.9,
            created_at=at,
            source_message_ids=(),
            source_type=MemorySourceType.MANUAL,
        )
    )


def test_due_opportunity_uses_persisted_user_activity_and_grounded_signal(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = RuntimeFixture(tmp_path)
        try:
            fixture.sessions.append_exchange(42, "以后继续聊这个。", "好。")
            last_user = fixture.sessions.last_user_message_at(42)
            assert last_user is not None
            await _seed_open_thread(fixture, at=last_user)
            now = last_user + timedelta(hours=3)

            evaluation = await fixture.runtime.evaluate(42, at=now)

            assert evaluation.due is True
            assert evaluation.should_prepare_delivery is True
            assert evaluation.decision.action is AutonomyAction.FOLLOW_UP
            assert evaluation.decision.selected_signal_id == "open-thread:thread_1"
            assert evaluation.opportunity is not None
            assert evaluation.opportunity.autonomy_messages_last_24h == 0
            assert len(fixture.provider.requests) == 1
            assert fixture.autonomy_store.last_opportunity_at(42, 1) == now
        finally:
            fixture.close()

    asyncio.run(scenario())


def test_schedule_not_due_skips_provider(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = RuntimeFixture(tmp_path)
        try:
            fixture.sessions.append_exchange(42, "以后继续聊这个。", "好。")
            last_user = fixture.sessions.last_user_message_at(42)
            assert last_user is not None
            await _seed_open_thread(fixture, at=last_user)
            first_at = last_user + timedelta(hours=3)
            await fixture.runtime.evaluate(42, at=first_at)
            second = await fixture.runtime.evaluate(42, at=first_at + timedelta(minutes=30))

            assert second.due is False
            assert second.decision.action is AutonomyAction.SILENT
            assert second.decision.reason_label == "schedule_not_due"
            assert len(fixture.provider.requests) == 1
        finally:
            fixture.close()

    asyncio.run(scenario())


def test_hard_guard_blocks_provider_when_user_suppressed(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = RuntimeFixture(tmp_path)
        try:
            fixture.sessions.append_exchange(42, "以后继续聊这个。", "好。")
            last_user = fixture.sessions.last_user_message_at(42)
            assert last_user is not None
            await _seed_open_thread(fixture, at=last_user)

            evaluation = await fixture.runtime.evaluate(
                42,
                at=last_user + timedelta(hours=3),
                user_suppressed=True,
            )

            assert evaluation.decision.action is AutonomyAction.SILENT
            assert evaluation.decision.reason_label == "guard_user_suppressed"
            assert fixture.provider.requests == []
        finally:
            fixture.close()

    asyncio.run(scenario())


def test_confirmed_delivery_updates_stats_only_after_explicit_record(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = RuntimeFixture(tmp_path)
        try:
            fixture.sessions.append_exchange(42, "以后继续聊这个。", "好。")
            last_user = fixture.sessions.last_user_message_at(42)
            assert last_user is not None
            await _seed_open_thread(fixture, at=last_user)
            now = last_user + timedelta(hours=3)
            evaluation = await fixture.runtime.evaluate(42, at=now)

            before = fixture.autonomy_store.delivery_stats(
                42,
                1,
                now=now,
                last_user_message_at=last_user,
            )
            assert before.autonomy_messages_last_24h == 0

            stored = fixture.runtime.record_confirmed_delivery(
                evaluation,
                at=now + timedelta(minutes=1),
            )
            after = fixture.autonomy_store.delivery_stats(
                42,
                1,
                now=now + timedelta(minutes=2),
                last_user_message_at=last_user,
            )
            assert stored.action is AutonomyAction.FOLLOW_UP
            assert after.autonomy_messages_last_24h == 1
            assert after.consecutive_unanswered_autonomy == 1
        finally:
            fixture.close()

    asyncio.run(scenario())


def test_generation_change_rejects_stale_delivery_confirmation(tmp_path: Path) -> None:
    async def scenario() -> None:
        fixture = RuntimeFixture(tmp_path)
        try:
            fixture.sessions.append_exchange(42, "以后继续聊这个。", "好。")
            last_user = fixture.sessions.last_user_message_at(42)
            assert last_user is not None
            await _seed_open_thread(fixture, at=last_user)
            evaluation = await fixture.runtime.evaluate(42, at=last_user + timedelta(hours=3))
            fixture.sessions.rotate(42)

            with pytest.raises(ValueError, match="generation changed"):
                fixture.runtime.record_confirmed_delivery(evaluation)
        finally:
            fixture.close()

    asyncio.run(scenario())


def test_confirmed_external_delivery_preserves_original_generation_after_send_race(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        fixture = RuntimeFixture(tmp_path)
        try:
            fixture.sessions.append_exchange(42, "以后继续聊这个。", "好。")
            last_user = fixture.sessions.last_user_message_at(42)
            assert last_user is not None
            await _seed_open_thread(fixture, at=last_user)
            evaluated_at = last_user + timedelta(hours=3)
            evaluation = await fixture.runtime.evaluate(42, at=evaluated_at)

            fixture.runtime.validate_delivery_candidate(evaluation)
            fixture.sessions.rotate(42)
            stored = fixture.runtime.record_confirmed_external_delivery(
                evaluation,
                at=evaluated_at + timedelta(minutes=1),
            )

            assert stored.generation == 1
            stats = fixture.autonomy_store.delivery_stats(
                42,
                1,
                now=evaluated_at + timedelta(minutes=2),
                last_user_message_at=last_user,
            )
            assert stats.autonomy_messages_last_24h == 1
        finally:
            fixture.close()

    asyncio.run(scenario())
