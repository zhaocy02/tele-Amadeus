from datetime import UTC, datetime

from amadeus_bot.llm import MessageRole
from amadeus_bot.runtime import ConversationSessionStore


def test_spontaneity_message_is_standalone_assistant_row_and_idempotent(tmp_path) -> None:
    store = ConversationSessionStore(tmp_path / "runtime.sqlite")
    at = datetime(2026, 9, 3, 2, 30, tzinfo=UTC)
    try:
        exchange = store.append_v2_exchange(
            42,
            "你确定没有其他问题了吗？",
            "大方向没问题。",
            turn_id="turn_source",
            policy_act="ADMIT_UNCERTAINTY",
        )
        before_count = store.message_count(42)

        first = store.append_v2_spontaneity_message(
            42,
            "……等等，我又想到一个边界条件。",
            generation=store.current_generation(42),
            source_turn_id="turn_source",
            telegram_message_id=9002,
            at=at,
        )
        duplicate = store.append_v2_spontaneity_message(
            42,
            "这个重复内容不应该产生新 row。",
            generation=store.current_generation(42),
            source_turn_id="turn_source",
            telegram_message_id=9003,
            at=at,
        )

        assert duplicate == first
        assert store.message_count(42) == before_count + 1
        records = store.history_records(42, 10)
        assert records[-1].message_id == first.assistant_message_id
        assert records[-1].role is MessageRole.ASSISTANT
        assert records[-1].content == "……等等，我又想到一个边界条件。"
        assert store.last_user_message_id(42) == exchange.user_message_id
    finally:
        store.close()
