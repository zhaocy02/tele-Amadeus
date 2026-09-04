from pathlib import Path

from amadeus_bot.llm import MessageRole
from amadeus_bot.runtime import ConversationSessionStore


def test_session_store_persists_history_and_rotation_hides_old_context(tmp_path: Path) -> None:
    filename = tmp_path / "runtime.sqlite"
    store = ConversationSessionStore(filename)
    try:
        store.append_exchange(7, "first", "reply-one")
        assert [(item.role, item.content) for item in store.history(7)] == [
            (MessageRole.USER, "first"),
            (MessageRole.ASSISTANT, "reply-one"),
        ]
        first_generation = store.current_generation(7)
        assert store.rotate(7) == first_generation + 1
        assert store.history(7) == ()
        store.append_exchange(7, "second", "reply-two")
    finally:
        store.close()

    reopened = ConversationSessionStore(filename)
    try:
        assert [item.content for item in reopened.history(7)] == ["second", "reply-two"]
    finally:
        reopened.close()
