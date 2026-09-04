from __future__ import annotations

from dataclasses import dataclass

from amadeus_bot.llm import LLMMessage, MessageRole


@dataclass(frozen=True, slots=True)
class WorkingMemoryWindow:
    """Bounded recent transcript view; not a second long-term persistence system."""

    messages: tuple[LLMMessage, ...] = ()
    max_messages: int = 40

    def __post_init__(self) -> None:
        if self.max_messages <= 0:
            raise ValueError("max_messages must be positive")
        invalid_roles = {
            message.role
            for message in self.messages
            if message.role not in {MessageRole.USER, MessageRole.ASSISTANT}
        }
        if invalid_roles:
            raise ValueError("working memory may contain only user/assistant transcript messages")
        if len(self.messages) > self.max_messages:
            object.__setattr__(self, "messages", self.messages[-self.max_messages :])

    def append_exchange(self, user_text: str, assistant_text: str) -> WorkingMemoryWindow:
        user = user_text.strip()
        assistant = assistant_text.strip()
        if not user or not assistant:
            raise ValueError("working-memory exchange text must not be empty")
        return WorkingMemoryWindow(
            messages=(
                *self.messages,
                LLMMessage(MessageRole.USER, user),
                LLMMessage(MessageRole.ASSISTANT, assistant),
            ),
            max_messages=self.max_messages,
        )

    def recent(self, limit: int | None = None) -> tuple[LLMMessage, ...]:
        if limit is None:
            return self.messages
        if limit < 0:
            raise ValueError("working-memory recent limit must not be negative")
        if limit == 0:
            return ()
        return self.messages[-limit:]
