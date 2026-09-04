import pytest

from amadeus_bot.llm import LLMMessage, MessageRole
from amadeus_bot.memory import WorkingMemoryWindow


def test_working_memory_keeps_bounded_actual_transcript() -> None:
    window = WorkingMemoryWindow(max_messages=4)
    window = window.append_exchange("u1", "a1")
    window = window.append_exchange("u2", "a2")
    window = window.append_exchange("u3", "a3")

    assert [message.content for message in window.messages] == ["u2", "a2", "u3", "a3"]
    assert [message.role for message in window.messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]


def test_working_memory_rejects_hidden_instruction_roles() -> None:
    with pytest.raises(ValueError, match="user/assistant"):
        WorkingMemoryWindow(
            messages=(LLMMessage(MessageRole.DEVELOPER, "do not persist this as transcript"),),
        )


def test_working_memory_recent_is_a_view_not_a_mutation() -> None:
    window = WorkingMemoryWindow(
        messages=(
            LLMMessage(MessageRole.USER, "u1"),
            LLMMessage(MessageRole.ASSISTANT, "a1"),
            LLMMessage(MessageRole.USER, "u2"),
        )
    )

    assert window.recent(2) == window.messages[-2:]
    assert window.recent(0) == ()
    assert len(window.messages) == 3
