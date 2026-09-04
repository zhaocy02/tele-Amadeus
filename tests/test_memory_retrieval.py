import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.memory import (
    MemoryKind,
    MemoryRecord,
    MemoryRetriever,
    MemorySourceType,
    StructuredMemoryRepository,
)

NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)


def _memory(
    memory_id: str,
    kind: MemoryKind,
    content: str,
    *,
    salience: float = 0.7,
    confidence: float = 0.9,
    age_days: int = 1,
    tags: tuple[str, ...] = (),
) -> MemoryRecord:
    source_type = (
        MemorySourceType.CHARACTER_INFERENCE
        if kind is MemoryKind.IMPRESSION
        else MemorySourceType.EXPLICIT_USER
    )
    return MemoryRecord(
        memory_id=memory_id,
        kind=kind,
        content=content,
        confidence=confidence,
        salience=salience,
        created_at=NOW - timedelta(days=age_days),
        source_message_ids=(100 + age_days,),
        source_type=source_type,
        tags=tags,
    )


def test_retrieval_prefers_lexically_relevant_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        try:
            await repository.upsert(
                _memory(
                    "amadeus",
                    MemoryKind.FACT,
                    "用户正在开发 Amadeus 的长期记忆系统。",
                    tags=("Amadeus", "memory"),
                )
            )
            await repository.upsert(
                _memory("pizza", MemoryKind.FACT, "用户曾经提到过披萨。", salience=0.95)
            )
            retriever = MemoryRetriever(repository)

            result = await retriever.retrieve("Amadeus 的记忆系统怎么设计？", at=NOW)

            assert result.memory_ids[0] == "amadeus"
            assert "pizza" not in result.memory_ids
            assert result.items[0].score.lexical > 0.0
        finally:
            repository.close()

    asyncio.run(scenario())


def test_retrieval_enforces_fact_group_quota(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        try:
            for index in range(3):
                await repository.upsert(
                    _memory(
                        f"fact-{index}",
                        MemoryKind.FACT,
                        f"Amadeus memory fact {index}",
                        salience=0.9 - index * 0.05,
                    )
                )
            retriever = MemoryRetriever(repository, min_score=0.0)

            result = await retriever.retrieve("Amadeus memory", at=NOW, limit=7)

            fact_items = [item for item in result.items if item.memory.kind is MemoryKind.FACT]
            assert len(fact_items) == 2
        finally:
            repository.close()

    asyncio.run(scenario())


def test_high_salience_recent_open_thread_can_surface_without_token_overlap(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        try:
            await repository.upsert(
                _memory(
                    "thread",
                    MemoryKind.OPEN_THREAD,
                    "用户说明天会告诉 Kurisu 实验结果。",
                    salience=0.95,
                    age_days=0,
                )
            )
            retriever = MemoryRetriever(repository)

            result = await retriever.retrieve("早上好", at=NOW)

            assert result.memory_ids == ("thread",)
            assert result.open_threads == ("[open_thread] 用户说明天会告诉 Kurisu 实验结果。",)
        finally:
            repository.close()

    asyncio.run(scenario())


def test_retrieval_result_preserves_memory_type_channels(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        try:
            memories = (
                _memory("fact", MemoryKind.FACT, "Amadeus 使用结构化记忆。"),
                _memory("relation", MemoryKind.RELATIONSHIP, "一起讨论过 Amadeus 架构。"),
                _memory(
                    "impression",
                    MemoryKind.IMPRESSION,
                    "Kurisu 觉得用户对 Amadeus 人格连续性很执着。",
                ),
                _memory("thread", MemoryKind.OPEN_THREAD, "继续讨论 Amadeus retrieval。"),
            )
            for memory in memories:
                await repository.upsert(memory)
            retriever = MemoryRetriever(repository, min_score=0.0)

            result = await retriever.retrieve("Amadeus", at=NOW)

            assert result.confirmed_facts == ("[fact] Amadeus 使用结构化记忆。",)
            assert result.relationship_memories == (
                "[relationship] 一起讨论过 Amadeus 架构。",
            )
            assert result.character_impressions == (
                "[impression] Kurisu 觉得用户对 Amadeus 人格连续性很执着。",
            )
            assert result.open_threads == ("[open_thread] 继续讨论 Amadeus retrieval。",)
        finally:
            repository.close()

    asyncio.run(scenario())


def test_mark_recalled_updates_only_selected_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        try:
            await repository.upsert(
                _memory("selected", MemoryKind.FACT, "Amadeus retrieval 使用透明评分。")
            )
            await repository.upsert(
                _memory("unrelated", MemoryKind.FACT, "用户提到过完全无关的话题。")
            )
            retriever = MemoryRetriever(repository)

            result = await retriever.retrieve(
                "Amadeus retrieval",
                at=NOW,
                mark_recalled=True,
            )
            selected = await repository.get("selected")
            unrelated = await repository.get("unrelated")

            assert result.memory_ids == ("selected",)
            assert selected is not None and selected.last_recalled_at == NOW
            assert unrelated is not None and unrelated.last_recalled_at is None
        finally:
            repository.close()

    asyncio.run(scenario())
