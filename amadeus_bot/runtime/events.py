from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4


class EventKind(StrEnum):
    USER_MESSAGE = "user_message"
    AUTONOMY_OPPORTUNITY = "autonomy_opportunity"
    RETROSPECTIVE_TICK = "retrospective_tick"
    OPEN_THREAD_DUE = "open_thread_due"
    STATE_DECAY = "state_decay"
    ADMIN_COMMAND = "admin_command"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """Program-owned immutable fact describing something that happened to the runtime."""

    kind: EventKind
    occurred_at: datetime
    event_id: str = field(default_factory=lambda: str(uuid4()))
    chat_id: int | None = None
    user_id: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def now(
        cls,
        kind: EventKind,
        *,
        chat_id: int | None = None,
        user_id: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RuntimeEvent:
        return cls(
            kind=kind,
            occurred_at=datetime.now(UTC),
            chat_id=chat_id,
            user_id=user_id,
            payload={} if payload is None else payload,
        )
