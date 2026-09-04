import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.runtime import (
    ConversationSessionStore,
    MemoryReviewLabel,
    RoutingReviewLabel,
    SQLiteTurnTelemetryStore,
    TurnTelemetryRecord,
    TurnTelemetryReporter,
)


def _record(
    *,
    turn_id: str,
    chat_id: int,
    observed_at: datetime,
    policy_mode: str,
    user_message_id: int,
    assistant_message_id: int,
    time_to_send_ms: int,
    queue_wait_ms: int = 0,
    retrieved_memory_ids: tuple[str, ...] = (),
) -> TurnTelemetryRecord:
    return TurnTelemetryRecord(
        turn_id=turn_id,
        chat_id=chat_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        telegram_message_id=100,
        observed_at=observed_at,
        policy_mode=policy_mode,  # type: ignore[arg-type]
        policy_act="DIRECT_ANSWER",
        policy_reason_label=("fast_explicit_task" if policy_mode == "fast" else "relationship"),
        retrieval_ms=4,
        policy_ms=0 if policy_mode == "fast" else 8000,
        context_ms=2,
        generation_ms=4000,
        character_total_ms=4002 if policy_mode == "fast" else 12002,
        time_to_send_ms=time_to_send_ms,
        finalize_ms=35,
        queue_wait_ms=queue_wait_ms,
        memory_enabled=True,
        retrieved_memory_ids=retrieved_memory_ids,
        retrospective_triggered=False,
        warnings=(),
        generator_model="fake-model",
        input_tokens=1000,
        output_tokens=120,
        total_tokens=1120,
    )


def test_turn_telemetry_links_to_transcript_without_copying_question_text(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime.sqlite"
    telemetry_path = tmp_path / "turn-telemetry.sqlite"
    sessions = ConversationSessionStore(runtime_path)
    telemetry = SQLiteTurnTelemetryStore(telemetry_path)
    try:
        exchange = sessions.append_v2_exchange(
            42,
            "你还记得我昨天说的那件事吗？",
            "记得一部分。",
            turn_id="turn_memory",
            policy_act="CALLBACK",
        )
        telemetry.record(
            _record(
                turn_id="turn_memory",
                chat_id=42,
                observed_at=datetime.now(UTC),
                policy_mode="llm",
                user_message_id=exchange.user_message_id,
                assistant_message_id=exchange.assistant_message_id,
                time_to_send_ms=12000,
                retrieved_memory_ids=("mem_1", "mem_2"),
            )
        )

        stored = telemetry.get("turn_memory")
        assert stored is not None
        observation = telemetry.observation(stored, runtime_db_path=runtime_path)
        assert observation.question_text == "你还记得我昨天说的那件事吗？"
        assert observation.telemetry.retrieved_memory_ids == ("mem_1", "mem_2")

        raw_db = sqlite3.connect(telemetry_path)
        try:
            rows = raw_db.execute("SELECT * FROM v2_turn_telemetry").fetchall()
            flattened = repr(rows)
            assert "你还记得我昨天说的那件事吗" not in flattened
            assert "记得一部分" not in flattened
        finally:
            raw_db.close()
    finally:
        telemetry.close()
        sessions.close()


def test_telemetry_report_summarizes_modes_questions_and_reviews(tmp_path: Path) -> None:
    runtime_path = tmp_path / "runtime.sqlite"
    sessions = ConversationSessionStore(runtime_path)
    telemetry = SQLiteTurnTelemetryStore(tmp_path / "turn-telemetry.sqlite")
    now = datetime.now(UTC)
    try:
        first = sessions.append_v2_exchange(
            42,
            "帮我解释一下摩尔浓度",
            "可以。",
            turn_id="turn_fast",
            policy_act="DIRECT_ANSWER",
        )
        second = sessions.append_v2_exchange(
            42,
            "你为什么刚才不愿意告诉我？",
            "因为……",
            turn_id="turn_llm",
            policy_act="EMOTIONAL_RESPONSE",
        )
        telemetry.record(
            _record(
                turn_id="turn_fast",
                chat_id=42,
                observed_at=now - timedelta(minutes=2),
                policy_mode="fast",
                user_message_id=first.user_message_id,
                assistant_message_id=first.assistant_message_id,
                time_to_send_ms=5000,
            )
        )
        telemetry.record(
            _record(
                turn_id="turn_llm",
                chat_id=42,
                observed_at=now - timedelta(minutes=1),
                policy_mode="llm",
                user_message_id=second.user_message_id,
                assistant_message_id=second.assistant_message_id,
                time_to_send_ms=13000,
                queue_wait_ms=700,
            )
        )
        review = telemetry.set_review(
            "turn_llm",
            routing_label=RoutingReviewLabel.CORRECT,
            memory_label=MemoryReviewLabel.NOT_APPLICABLE,
            note="relationship/disclosure turn should remain deliberate",
            at=now,
        )
        assert review.routing_label is RoutingReviewLabel.CORRECT

        reporter = TurnTelemetryReporter(telemetry, runtime_db_path=runtime_path)
        summary = reporter.summarize(now - timedelta(hours=1), chat_id=42)
        assert summary.total_turns == 2
        assert summary.fast_turns == 1
        assert summary.llm_turns == 1
        assert summary.fast_response_wait is not None
        assert summary.fast_response_wait.p50 == 5000
        assert summary.llm_response_wait is not None
        assert summary.llm_response_wait.p50 == 13700

        rendered = reporter.render(
            now - timedelta(hours=1),
            since_label="1h",
            chat_id=42,
            routing_limit=10,
            slowest=1,
        )
        assert "fast=1 (50.0%)" in rendered
        assert "llm=1 (50.0%)" in rendered
        assert "routing_reviews=correct:1" in rendered
        assert "memory_reviews=not_applicable:1" in rendered
        assert "帮我解释一下摩尔浓度" in rendered
        assert "你为什么刚才不愿意告诉我" in rendered
        assert "Slowest visible replies" in rendered
    finally:
        telemetry.close()
        sessions.close()


def test_review_turn_accepts_unique_prefix_and_preserves_other_fields(tmp_path: Path) -> None:
    telemetry = SQLiteTurnTelemetryStore(tmp_path / "turn-telemetry.sqlite")
    now = datetime.now(UTC)
    try:
        telemetry.record(
            _record(
                turn_id="turn_abcdef123456",
                chat_id=42,
                observed_at=now,
                policy_mode="fast",
                user_message_id=1,
                assistant_message_id=2,
                time_to_send_ms=4000,
            )
        )
        telemetry.set_review(
            "turn_abcdef",
            routing_label=RoutingReviewLabel.FALSE_FAST,
            note="shared-history cue should have been deliberate",
            at=now,
        )
        review = telemetry.set_review(
            "turn_abcdef",
            memory_label=MemoryReviewLabel.RETRIEVAL_MISS,
            at=now + timedelta(seconds=1),
        )
        assert review.routing_label is RoutingReviewLabel.FALSE_FAST
        assert review.memory_label is MemoryReviewLabel.RETRIEVAL_MISS
        assert review.note == "shared-history cue should have been deliberate"
    finally:
        telemetry.close()
