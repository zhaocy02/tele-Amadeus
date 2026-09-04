from pathlib import Path

import pytest

from amadeus_bot.memory import (
    LegacyMemoryKind,
    LegacyMemoryStore,
    derive_automatic_memories,
)


def test_legacy_memory_store_preserves_v1_commands_and_schema_semantics(tmp_path: Path) -> None:
    store = LegacyMemoryStore(tmp_path / "memory.sqlite")
    try:
        item = store.remember(42, "我喜欢黑咖啡")
        assert item.memory_id > 0
        assert item.kind is LegacyMemoryKind.EXPLICIT
        assert store.list(42)[0].content == "我喜欢黑咖啡"

        retrieved = store.retrieve(42, "咖啡")
        assert retrieved and retrieved[0].memory_id == item.memory_id

        store.set_enabled(42, False)
        assert not store.is_enabled(42)
        assert store.retrieve(42, "咖啡") == ()
        assert store.list(42)[0].memory_id == item.memory_id

        removed = store.forget(42, str(item.memory_id))
        assert tuple(memory.memory_id for memory in removed) == (item.memory_id,)
        assert store.list(42) == ()
    finally:
        store.close()


def test_legacy_memory_store_rejects_sensitive_content(tmp_path: Path) -> None:
    store = LegacyMemoryStore(tmp_path / "memory.sqlite")
    try:
        with pytest.raises(ValueError, match="敏感信息"):
            store.remember(1, "我的 API key 是 abcdef")
    finally:
        store.close()


def test_legacy_automatic_memory_rules_match_current_v1_categories() -> None:
    candidates = derive_automatic_memories("我叫小明。我喜欢咖啡。我计划明天跑步。")

    assert [candidate.kind for candidate in candidates] == [
        LegacyMemoryKind.PROFILE,
        LegacyMemoryKind.PREFERENCE,
        LegacyMemoryKind.COMMITMENT,
    ]
