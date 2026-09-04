from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class MessageRole(StrEnum):
    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class LLMImage:
    """Ephemeral provider-neutral image input for a single LLM request."""

    data: bytes
    media_type: str = "image/jpeg"

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("LLM image data must not be empty")
        if self.media_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise ValueError("unsupported LLM image media type")


@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: MessageRole
    content: str
    images: tuple[LLMImage, ...] = ()

    def __post_init__(self) -> None:
        if self.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER} and self.images:
            raise ValueError("system/developer messages may not contain images")


@dataclass(frozen=True, slots=True)
class LLMToolDefinition:
    """Provider-neutral function tool exposed to one LLM generation turn."""

    name: str
    description: str
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("LLM tool name must not be empty")
        if not self.description.strip():
            raise ValueError("LLM tool description must not be empty")
        if self.parameters.get("type") != "object":
            raise ValueError("LLM tool parameters must use an object JSON schema")


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    """One function call requested by the model."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    item_id: str | None = None

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("LLM tool call id must not be empty")
        if not self.name.strip():
            raise ValueError("LLM tool call name must not be empty")


@dataclass(frozen=True, slots=True)
class LLMToolResult:
    """Runtime output corresponding to one model-requested function call."""

    call_id: str
    output: str

    def __post_init__(self) -> None:
        if not self.call_id.strip():
            raise ValueError("LLM tool result call id must not be empty")
        if not self.output.strip():
            raise ValueError("LLM tool result output must not be empty")


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[LLMMessage, ...]
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tools: tuple[LLMToolDefinition, ...] = ()
    tool_results: tuple[LLMToolResult, ...] = ()
    continuation: object | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    model: str
    request_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: tuple[LLMToolCall, ...] = ()
    continuation: object | None = None


class LLMProvider(Protocol):
    """Provider boundary intentionally independent of CPA/OpenAI implementation details."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        ...
