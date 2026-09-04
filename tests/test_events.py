from datetime import UTC, datetime

import pytest

from amadeus_bot.runtime import EventKind, RuntimeEvent


def test_runtime_event_copies_payload_into_immutable_mapping() -> None:
    payload = {"text": "hello"}
    event = RuntimeEvent(
        kind=EventKind.USER_MESSAGE,
        occurred_at=datetime.now(UTC),
        chat_id=10,
        user_id=20,
        payload=payload,
    )
    payload["text"] = "mutated"

    assert event.payload["text"] == "hello"
    with pytest.raises(TypeError):
        event.payload["text"] = "forbidden"  # type: ignore[index]


def test_runtime_event_requires_timezone_aware_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RuntimeEvent(kind=EventKind.USER_MESSAGE, occurred_at=datetime.now())


def test_now_creates_program_owned_event_identity() -> None:
    event = RuntimeEvent.now(EventKind.AUTONOMY_OPPORTUNITY)

    assert event.event_id
    assert event.occurred_at.tzinfo is not None
