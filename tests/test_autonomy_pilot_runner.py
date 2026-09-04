import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from amadeus_bot.character import (
    AutonomyAction,
    AutonomyDecision,
    AutonomyOpportunity,
    AutonomySignal,
    AutonomySignalKind,
)
from amadeus_bot.runtime import (
    AutonomyRuntimeEvaluation,
    SQLiteRuntimePreferenceStore,
)
from amadeus_bot.telegram import V2AutonomyPilotRunner


class FakeConversation:
    def __init__(self) -> None:
        self.active = False

    def has_active_turn(self, chat_id: int) -> bool:
        return self.active


class FakeAutonomy:
    def __init__(self, evaluation: AutonomyRuntimeEvaluation) -> None:
        self.evaluation = evaluation
        self.calls: list[tuple[int, datetime, bool, bool, bool, datetime | None]] = []

    async def evaluate(
        self,
        chat_id: int,
        *,
        at: datetime,
        do_not_disturb: bool,
        user_suppressed: bool,
        sleep_mode: bool,
        sleep_started_at: datetime | None,
    ) -> AutonomyRuntimeEvaluation:
        self.calls.append(
            (
                chat_id,
                at,
                do_not_disturb,
                user_suppressed,
                sleep_mode,
                sleep_started_at,
            )
        )
        return self.evaluation


class FakeDelivery:
    def __init__(self) -> None:
        self.prepared: list[AutonomyRuntimeEvaluation] = []

    async def prepare_autonomy_message(self, evaluation: AutonomyRuntimeEvaluation) -> str:
        self.prepared.append(evaluation)
        return "之后那个话题……我刚好又想到一点。"


class FakeRouter:
    def __init__(self) -> None:
        self.delivered: list[tuple[AutonomyRuntimeEvaluation, str]] = []

    async def deliver_prepared_autonomy(
        self,
        evaluation: AutonomyRuntimeEvaluation,
        text: str,
    ) -> SimpleNamespace:
        self.delivered.append((evaluation, text))
        return SimpleNamespace(telegram_message_id=42, warnings=())


def _evaluation(now: datetime, *, action: AutonomyAction) -> AutonomyRuntimeEvaluation:
    signal = AutonomySignal(
        signal_id="open-thread:test",
        kind=AutonomySignalKind.OPEN_THREAD,
        summary="之前留下了一个可以继续讨论的话题。",
        salience=0.9,
        source_thread_id="test",
    )
    if action is AutonomyAction.SILENT:
        decision = AutonomyDecision(
            action=AutonomyAction.SILENT,
            motivation=0.0,
            reason_label="guard_do_not_disturb",
        )
    else:
        decision = AutonomyDecision(
            action=action,
            selected_signal_id=signal.signal_id,
            motivation=0.9,
            focus="自然地接回这个话题",
            reason_label="grounded_follow_up",
        )
    return AutonomyRuntimeEvaluation(
        chat_id=10,
        generation=1,
        evaluated_at=now,
        due=True,
        opportunity=AutonomyOpportunity(
            now=now,
            last_user_message_at=now - timedelta(hours=4),
            signals=(signal,),
        ),
        decision=decision,
        evaluation_id=7,
    )


def test_disabled_chat_never_evaluates(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
        autonomy = FakeAutonomy(_evaluation(now, action=AutonomyAction.SILENT))
        delivery = FakeDelivery()
        router = FakeRouter()
        conversation = FakeConversation()
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        try:
            runner = V2AutonomyPilotRunner(
                autonomy=cast(Any, autonomy),
                delivery=cast(Any, delivery),
                router=cast(Any, router),
                conversation=cast(Any, conversation),
                preferences=preferences,
                allowed_chat_ids=frozenset({10}),
                timezone="Asia/Shanghai",
            )
            summary = await runner.tick(at=now)
            assert summary.considered_chats == 1
            assert summary.evaluated_opportunities == 0
            assert autonomy.calls == []
        finally:
            preferences.close()

    asyncio.run(scenario())


def test_control_change_enforces_one_minute_cooldown(tmp_path: Path) -> None:
    async def scenario() -> None:
        autonomy = FakeAutonomy(
            _evaluation(datetime.now(UTC), action=AutonomyAction.SILENT)
        )
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        try:
            preferences.set_autonomy_enabled(10, True)
            changed = preferences.autonomy_preferences(10)
            assert changed.updated_at is not None
            runner = V2AutonomyPilotRunner(
                autonomy=cast(Any, autonomy),
                delivery=cast(Any, FakeDelivery()),
                router=cast(Any, FakeRouter()),
                conversation=cast(Any, FakeConversation()),
                preferences=preferences,
                allowed_chat_ids=frozenset({10}),
                timezone="Asia/Shanghai",
            )
            await runner.tick(at=changed.updated_at + timedelta(seconds=59))
            assert autonomy.calls == []
            await runner.tick(at=changed.updated_at + timedelta(minutes=1))
            assert len(autonomy.calls) == 1
        finally:
            preferences.close()

    asyncio.run(scenario())


def test_semantic_sleep_state_flows_into_runtime_without_dnd(tmp_path: Path) -> None:
    async def scenario() -> None:
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        try:
            preferences.set_autonomy_enabled(10, True)
            changed = preferences.autonomy_preferences(10)
            assert changed.updated_at is not None
            bedtime = changed.updated_at + timedelta(minutes=2)
            assert preferences.observe_user_text(10, "晚安", at=bedtime) == "sleep_started"
            evaluation = _evaluation(bedtime + timedelta(minutes=1), action=AutonomyAction.SILENT)
            autonomy = FakeAutonomy(evaluation)
            runner = V2AutonomyPilotRunner(
                autonomy=cast(Any, autonomy),
                delivery=cast(Any, FakeDelivery()),
                router=cast(Any, FakeRouter()),
                conversation=cast(Any, FakeConversation()),
                preferences=preferences,
                allowed_chat_ids=frozenset({10}),
                timezone="Asia/Shanghai",
            )
            await runner.tick(at=bedtime + timedelta(minutes=1))
            call = autonomy.calls[0]
            assert call[2] is False
            assert call[4] is True
            assert call[5] == bedtime
        finally:
            preferences.close()

    asyncio.run(scenario())


def test_grounded_non_silent_candidate_is_prepared_then_delivered(tmp_path: Path) -> None:
    async def scenario() -> None:
        preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        try:
            preferences.set_autonomy_enabled(10, True)
            changed = preferences.autonomy_preferences(10)
            assert changed.updated_at is not None
            now = changed.updated_at + timedelta(minutes=2)
            evaluation = _evaluation(now, action=AutonomyAction.FOLLOW_UP)
            autonomy = FakeAutonomy(evaluation)
            delivery = FakeDelivery()
            router = FakeRouter()
            runner = V2AutonomyPilotRunner(
                autonomy=cast(Any, autonomy),
                delivery=cast(Any, delivery),
                router=cast(Any, router),
                conversation=cast(Any, FakeConversation()),
                preferences=preferences,
                allowed_chat_ids=frozenset({10}),
                timezone="Asia/Shanghai",
            )
            summary = await runner.tick(at=now)
            assert summary.evaluated_opportunities == 1
            assert summary.non_silent_candidates == 1
            assert summary.confirmed_deliveries == 1
            assert delivery.prepared == [evaluation]
            assert router.delivered[0][0] == evaluation
        finally:
            preferences.close()

    asyncio.run(scenario())
