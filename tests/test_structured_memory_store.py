import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    MemoryStatus,
    StructuredMemoryRepository,
)


def _record(
    memory_id: str,
    content: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    salience: float = 0.75,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        kind=kind,
        content=content,
        confidence=0.9,
        salience=salience,
        created_at=created_at or datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        source_message_ids=(101, 102),
        source_type=(
            MemorySourceType.CHARACTER_INFERENCE
            if kind is MemoryKind.IMPRESSION
            else MemorySourceType.EXPLICIT_USER
        ),
        expires_at=expires_at,
        tags=("amadeus",),
        entities=("Amadeus",),
    )


def test_structured_repository_round_trips_provenance(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory-v2.sqlite")
        try:
            memory = _record("mem_1", "用户明确说正在重构 Amadeus。")
            await repository.upsert(memory)
            loaded = await repository.get("mem_1")

            assert loaded is not None
            assert loaded.kind is MemoryKind.FACT
            assert loaded.source_type is MemorySourceType.EXPLICIT_USER
            assert loaded.source_message_ids == (101, 102)
            assert loaded.tags == ("amadeus",)
            assert loaded.entities == ("Amadeus",)
        finally:
            repository.close()

    asyncio.run(scenario())


def test_list_active_excludes_rejected_and_expired_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory-v2.sqlite")
        now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        try:
            await repository.upsert(_record("active", "仍然有效", created_at=now))
            await repository.upsert(
                _record(
                    "expired",
                    "只在更早时间有效",
                    created_at=now - timedelta(days=1),
                    expires_at=now - timedelta(minutes=1),
                )
            )
            await repository.upsert(_record("rejected", "后来被用户纠正", created_at=now))
            await repository.set_status("rejected", MemoryStatus.REJECTED, at=now)

            active = await repository.list_active(at=now)
            assert [memory.memory_id for memory in active] == ["active"]
        finally:
            repository.close()

    asyncio.run(scenario())


def test_expired_records_do_not_consume_active_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory-v2.sqlite")
        now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        try:
            await repository.upsert(
                _record(
                    "expired-high",
                    "高显著性但已经过期",
                    expires_at=now - timedelta(seconds=1),
                    salience=1.0,
                )
            )
            await repository.upsert(_record("active-low", "仍然有效", salience=0.2))

            active = await repository.list_active(limit=1, at=now)
            assert [memory.memory_id for memory in active] == ["active-low"]
        finally:
            repository.close()

    asyncio.run(scenario())


def test_supersede_preserves_old_record_and_links_replacement(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory-v2.sqlite")
        now = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
        try:
            old = _record("old", "Kurisu 以为用户是在熬夜。", kind=MemoryKind.IMPRESSION)
            replacement = _record(
                "new",
                "用户说明深夜在线是因为时差。",
                kind=MemoryKind.FACT,
                created_at=now,
            )
            await repository.upsert(old)
            saved = await repository.supersede("old", replacement, at=now)

            old_after = await repository.get("old")
            new_after = await repository.get("new")
            assert old_after is not None
            assert old_after.status is MemoryStatus.SUPERSEDED
            assert new_after == saved
            assert new_after is not None
            assert new_after.supersedes_id == "old"
            assert new_after.status is MemoryStatus.ACTIVE
        finally:
            repository.close()

    asyncio.run(scenario())


def test_touch_recalled_updates_only_selected_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory-v2.sqlite")
        recalled_at = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
        try:
            await repository.upsert(_record("one", "one"))
            await repository.upsert(_record("two", "two"))
            await repository.touch_recalled(("two",), at=recalled_at)

            one = await repository.get("one")
            two = await repository.get("two")
            assert one is not None and one.last_recalled_at is None
            assert two is not None and two.last_recalled_at == recalled_at
        finally:
            repository.close()

    asyncio.run(scenario())
