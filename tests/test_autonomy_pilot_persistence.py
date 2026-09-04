from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from amadeus_bot.character import (
    AutonomyAction,
    AutonomyDecision,
    AutonomyOpportunity,
    AutonomySignal,
    AutonomySignalKind,
    SQLiteCharacterStateStore,
)
from amadeus_bot.memory import StructuredMemoryRepository
from amadeus_bot.runtime import (
    AutonomyRuntimeCoordinator,
    AutonomyRuntimeEvaluation,
    ConversationSessionStore,
    SQLiteAutonomyRuntimeStore,
)


def test_autonomy_evaluation_and_delivery_link_are_persisted(tmp_path: Path) -> None:
    store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
    try:
        now = datetime.now(UTC)
        evaluation = store.record_evaluation(
            10,
            1,
            action=AutonomyAction.FOLLOW_UP,
            reason_label="grounded_follow_up",
            selected_signal_id="open-thread:test",
            motivation=0.9,
            at=now,
        )
        delivery = store.record_delivery(
            10,
            1,
            action=AutonomyAction.FOLLOW_UP,
            signal_id="open-thread:test",
            at=now + timedelta(seconds=1),
            evaluation_id=evaluation.evaluation_id,
        )

        loaded = store.last_evaluation(10, 1)
        assert loaded == evaluation
        assert delivery.evaluation_id == evaluation.evaluation_id
    finally:
        store.close()


class NeverPlanner:
    async def decide(self, opportunity: AutonomyOpportunity) -> AutonomyDecision:
        raise AssertionError("planner should not run in stale-delivery test")


class NeverScheduler:
    def is_due(self, *, now: datetime, last_opportunity_at: datetime | None) -> bool:
        raise AssertionError("scheduler should not run in stale-delivery test")


def test_user_activity_invalidates_prepared_autonomy_candidate(tmp_path: Path) -> None:
    memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
    state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
    sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
    autonomy_store = SQLiteAutonomyRuntimeStore(tmp_path / "autonomy.sqlite")
    try:
        sessions.append_exchange(10, "先聊到这里", "好。")
        last_user = sessions.last_user_message_at(10)
        last_user_id = sessions.last_user_message_id(10)
        assert last_user is not None
        assert last_user_id is not None
        now = last_user + timedelta(hours=4)
        signal = AutonomySignal(
            signal_id="open-thread:test",
            kind=AutonomySignalKind.OPEN_THREAD,
            summary="之前有个话题还没说完。",
            salience=0.9,
            source_thread_id="test",
        )
        evaluation = AutonomyRuntimeEvaluation(
            chat_id=10,
            generation=sessions.current_generation(10),
            evaluated_at=now,
            due=True,
            opportunity=AutonomyOpportunity(
                now=now,
                last_user_message_at=last_user,
                signals=(signal,),
            ),
            decision=AutonomyDecision(
                action=AutonomyAction.FOLLOW_UP,
                selected_signal_id=signal.signal_id,
                motivation=0.9,
                focus="自然接回这个话题",
                reason_label="grounded_follow_up",
            ),
            last_user_message_id=last_user_id,
        )
        coordinator = AutonomyRuntimeCoordinator(
            planner=cast(Any, NeverPlanner()),
            scheduler=cast(Any, NeverScheduler()),
            memory_repository=memory,
            state_store=state,
            sessions=sessions,
            autonomy_store=autonomy_store,
        )
        coordinator.validate_delivery_candidate(evaluation)

        sessions.append_exchange(10, "我又发了一条新消息", "收到。")
        assert sessions.last_user_message_id(10) != last_user_id
        with pytest.raises(ValueError, match="user activity changed"):
            coordinator.validate_delivery_candidate(evaluation)
    finally:
        memory.close()
        state.close()
        sessions.close()
        autonomy_store.close()
