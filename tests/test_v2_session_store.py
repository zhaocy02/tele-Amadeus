import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from amadeus_bot.llm import MessageRole
from amadeus_bot.runtime import ConversationSessionStore


def test_v2_exchange_persists_message_ids_and_policy_atomically(tmp_path: Path) -> None:
    store = ConversationSessionStore(tmp_path / "runtime.sqlite")
    try:
        exchange = store.append_v2_exchange(
            42,
            "user text",
            "assistant text",
            turn_id="turn_1",
            policy_act="DIRECT_ANSWER",
        )

        records = store.history_records(42, 10)
        assert tuple(record.message_id for record in records) == (
            exchange.user_message_id,
            exchange.assistant_message_id,
        )
        assert tuple(record.role for record in records) == (
            MessageRole.USER,
            MessageRole.ASSISTANT,
        )
        assert store.recent_policy_acts(42) == ("DIRECT_ANSWER",)
    finally:
        store.close()


def test_duplicate_turn_id_rolls_back_duplicate_transcript(tmp_path: Path) -> None:
    store = ConversationSessionStore(tmp_path / "runtime.sqlite")
    try:
        store.append_v2_exchange(
            42,
            "first user",
            "first assistant",
            turn_id="turn_same",
            policy_act="SHORT_ANSWER",
        )

        with pytest.raises(sqlite3.IntegrityError):
            store.append_v2_exchange(
                42,
                "duplicate user",
                "duplicate assistant",
                turn_id="turn_same",
                policy_act="DIRECT_ANSWER",
            )

        records = store.history_records(42, 10)
        assert tuple(record.content for record in records) == (
            "first user",
            "first assistant",
        )
        assert store.message_count(42) == 2
        assert store.recent_policy_acts(42) == ("SHORT_ANSWER",)
    finally:
        store.close()


def test_rotation_separates_v2_policy_history(tmp_path: Path) -> None:
    store = ConversationSessionStore(tmp_path / "runtime.sqlite")
    try:
        store.append_v2_exchange(
            42,
            "first",
            "reply",
            turn_id="turn_1",
            policy_act="TEASE",
        )
        store.rotate(42)

        assert store.history(42) == ()
        assert store.recent_policy_acts(42) == ()
    finally:
        store.close()


def test_retrospective_marker_tracks_delivered_turns_per_generation(tmp_path: Path) -> None:
    store = ConversationSessionStore(tmp_path / "runtime.sqlite")
    try:
        for index in range(2):
            store.append_v2_exchange(
                42,
                f"user {index}",
                f"assistant {index}",
                turn_id=f"turn_{index}",
                policy_act="DIRECT_ANSWER",
            )

        assert store.v2_turn_count(42) == 2
        assert store.retrospective_turns_since_last(42) == 2

        store.mark_retrospective_run(
            42,
            at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        )
        assert store.retrospective_turns_since_last(42) == 0

        store.append_v2_exchange(
            42,
            "third",
            "third reply",
            turn_id="turn_3",
            policy_act="TEASE",
        )
        assert store.retrospective_turns_since_last(42) == 1

        store.rotate(42)
        assert store.v2_turn_count(42) == 0
        assert store.retrospective_turns_since_last(42) == 0
    finally:
        store.close()


def test_delivered_autonomy_message_is_assistant_only_and_idempotent(tmp_path: Path) -> None:
    store = ConversationSessionStore(tmp_path / "runtime.sqlite")
    try:
        store.append_v2_exchange(
            42,
            "以后再继续聊。",
            "好。",
            turn_id="turn_1",
            policy_act="SHORT_ANSWER",
        )
        at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
        first = store.append_v2_autonomy_message(
            42,
            "那个话题，你还想继续的话我可以听听。",
            generation=1,
            action="FOLLOW_UP",
            signal_id="open-thread:thread_1",
            telegram_message_id=9001,
            at=at,
        )
        duplicate = store.append_v2_autonomy_message(
            42,
            "这个文本在 duplicate path 不应产生第二行。",
            generation=1,
            action="FOLLOW_UP",
            signal_id="open-thread:thread_1",
            telegram_message_id=9001,
            at=at,
        )

        assert duplicate == first
        records = store.history_records(42, 10)
        assert tuple(record.role for record in records) == (
            MessageRole.USER,
            MessageRole.ASSISTANT,
            MessageRole.ASSISTANT,
        )
        assert records[-1].content == "那个话题，你还想继续的话我可以听听。"
        assert store.message_count(42) == 3
        assert store.v2_turn_count(42) == 1
        assert store.recent_policy_acts(42) == ("SHORT_ANSWER",)
    finally:
        store.close()
