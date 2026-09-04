from datetime import UTC, datetime, timedelta

import pytest

from amadeus_bot.memory import MemoryKind, MemoryRecord, MemorySourceType, MemoryStatus


def _record(**overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "memory_id": "mem_1",
        "kind": MemoryKind.FACT,
        "content": "用户明确说自己正在开发 Amadeus。",
        "confidence": 0.95,
        "salience": 0.7,
        "created_at": datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        "source_message_ids": (101,),
        "source_type": MemorySourceType.EXPLICIT_USER,
    }
    values.update(overrides)
    return MemoryRecord(**values)  # type: ignore[arg-type]


def test_character_inference_cannot_be_promoted_to_confirmed_fact() -> None:
    with pytest.raises(ValueError, match="confirmed fact"):
        _record(source_type=MemorySourceType.CHARACTER_INFERENCE)


def test_character_inference_is_valid_as_impression() -> None:
    memory = _record(
        kind=MemoryKind.IMPRESSION,
        content="Kurisu 怀疑用户最近又在熬夜。",
        confidence=0.55,
        source_type=MemorySourceType.CHARACTER_INFERENCE,
    )

    assert memory.kind is MemoryKind.IMPRESSION
    assert memory.source_type is MemorySourceType.CHARACTER_INFERENCE


def test_semantic_memory_requires_message_provenance() -> None:
    with pytest.raises(ValueError, match="provenance"):
        _record(source_message_ids=())

    migrated = _record(
        source_message_ids=(),
        source_type=MemorySourceType.MIGRATED,
    )
    assert migrated.source_message_ids == ()


def test_memory_rejects_duplicate_source_message_ids() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        _record(source_message_ids=(101, 101))


def test_expired_memory_is_not_active_without_destroying_record() -> None:
    created = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    memory = _record(expires_at=created + timedelta(hours=1))

    assert memory.is_active(at=created + timedelta(minutes=30))
    assert not memory.is_active(at=created + timedelta(hours=2))
    assert memory.status is MemoryStatus.ACTIVE


def test_memory_correction_links_cannot_reference_self() -> None:
    with pytest.raises(ValueError, match="itself"):
        _record(supersedes_id="mem_1")
