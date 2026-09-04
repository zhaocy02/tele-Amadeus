import asyncio
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.llm import LLMRequest, LLMResponse
from amadeus_bot.memory import (
    ARCHIVIST_PROMPT_VERSION,
    ArchivistAction,
    ArchivistContext,
    MemoryArchivist,
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
    MemoryStatus,
    StructuredMemoryRepository,
)


class FixedProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.text, model="test-archivist-model")


def _context(*, candidate_memories: tuple[MemoryRecord, ...] = ()) -> ArchivistContext:
    return ArchivistContext(
        user_message_id=101,
        assistant_message_id=102,
        user_text="我不是因为项目熬夜，是因为时差。",
        assistant_text="……原来是时差。那我刚才的判断确实错了。",
        candidate_memories=candidate_memories,
    )


def _old_impression() -> MemoryRecord:
    return MemoryRecord(
        memory_id="mem_old",
        kind=MemoryKind.IMPRESSION,
        content="Kurisu 怀疑用户为了项目熬夜。",
        confidence=0.55,
        salience=0.7,
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        source_message_ids=(90, 91),
        source_type=MemorySourceType.CHARACTER_INFERENCE,
    )


def test_invalid_archivist_output_falls_back_to_no_write() -> None:
    provider = FixedProvider("not json")
    archivist = MemoryArchivist(provider=provider)

    decision = asyncio.run(archivist.analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE
    assert decision.items == ()
    assert decision.reason_label == "archivist_failure"
    assert provider.requests[0].metadata["prompt_version"] == ARCHIVIST_PROMPT_VERSION


def test_archivist_rejects_invented_source_message_ids() -> None:
    provider = FixedProvider(
        """{
          "decision": "CREATE",
          "items": [{
            "kind": "fact",
            "content": "用户说深夜在线是时差导致的。",
            "confidence": 0.95,
            "salience": 0.7,
            "source_message_ids": [999],
            "evidence": "explicit_user",
            "target_memory_id": null,
            "expires_at": null,
            "tags": ["timezone"],
            "entities": []
          }],
          "reason_label": "correction"
        }"""
    )
    archivist = MemoryArchivist(provider=provider)

    decision = asyncio.run(archivist.analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE
    assert decision.reason_label == "archivist_items_rejected"


def test_archivist_rejects_character_inference_as_fact() -> None:
    provider = FixedProvider(
        """{
          "decision": "CREATE",
          "items": [{
            "kind": "fact",
            "content": "用户大概经常熬夜。",
            "confidence": 0.6,
            "salience": 0.5,
            "source_message_ids": [101],
            "evidence": "character_inference",
            "target_memory_id": null,
            "expires_at": null,
            "tags": [],
            "entities": []
          }],
          "reason_label": "guess"
        }"""
    )
    archivist = MemoryArchivist(provider=provider)

    decision = asyncio.run(archivist.analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE


def test_archivist_rejects_sensitive_automatic_memory() -> None:
    provider = FixedProvider(
        """{
          "decision": "CREATE",
          "items": [{
            "kind": "fact",
            "content": "用户的 API key 是 secret-value。",
            "confidence": 1.0,
            "salience": 1.0,
            "source_message_ids": [101],
            "evidence": "explicit_user",
            "target_memory_id": null,
            "expires_at": null,
            "tags": [],
            "entities": []
          }],
          "reason_label": "credential"
        }"""
    )
    archivist = MemoryArchivist(provider=provider)

    decision = asyncio.run(archivist.analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE


def test_valid_create_is_program_validated_then_persisted(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = FixedProvider(
            """{
              "decision": "CREATE",
              "items": [{
                "kind": "fact",
                "content": "用户明确说深夜在线是因为时差。",
                "confidence": 0.96,
                "salience": 0.75,
                "source_message_ids": [101],
                "evidence": "explicit_user",
                "target_memory_id": null,
                "expires_at": null,
                "tags": ["timezone"],
                "entities": []
              }],
              "reason_label": "explicit_correction"
            }"""
        )
        repository = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        archivist = MemoryArchivist(provider=provider, id_factory=lambda: "mem_new")
        try:
            result = await archivist.process(
                context=_context(),
                repository=repository,
                at=datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
            )
            saved = await repository.get("mem_new")

            assert saved is not None
            assert saved.kind is MemoryKind.FACT
            assert saved.source_type is MemorySourceType.EXPLICIT_USER
            assert saved.source_message_ids == (101,)
            assert saved.tags == ("timezone",)
            assert result.written_records == (saved,)
        finally:
            repository.close()

    asyncio.run(scenario())


def test_supersede_preserves_wrong_impression_as_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        old = _old_impression()
        provider = FixedProvider(
            """{
              "decision": "SUPERSEDE",
              "items": [{
                "kind": "fact",
                "content": "用户说明深夜在线是因为时差。",
                "confidence": 0.96,
                "salience": 0.8,
                "source_message_ids": [101],
                "evidence": "explicit_user",
                "target_memory_id": "mem_old",
                "expires_at": null,
                "tags": ["timezone"],
                "entities": []
              }],
              "reason_label": "user_correction"
            }"""
        )
        repository = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        archivist = MemoryArchivist(provider=provider, id_factory=lambda: "mem_corrected")
        try:
            await repository.upsert(old)
            result = await archivist.process(
                context=_context(candidate_memories=(old,)),
                repository=repository,
                at=datetime(2026, 9, 1, 13, 0, tzinfo=UTC),
            )
            old_after = await repository.get("mem_old")
            corrected = await repository.get("mem_corrected")

            assert old_after is not None
            assert old_after.status is MemoryStatus.SUPERSEDED
            assert corrected is not None
            assert corrected.supersedes_id == "mem_old"
            assert result.superseded_memory_ids == ("mem_old",)
        finally:
            repository.close()

    asyncio.run(scenario())
