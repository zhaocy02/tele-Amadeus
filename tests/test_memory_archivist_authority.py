import asyncio
from datetime import UTC, datetime

from amadeus_bot.llm import LLMRequest, LLMResponse
from amadeus_bot.memory import (
    ArchivistAction,
    ArchivistContext,
    MemoryArchivist,
    MemoryKind,
    MemoryRecord,
    MemorySourceType,
)


class FixedProvider:
    def __init__(self, text: str) -> None:
        self.text = text

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.text, model="test-archivist-model")


def _context(*, candidates: tuple[MemoryRecord, ...] = ()) -> ArchivistContext:
    return ArchivistContext(
        user_message_id=101,
        assistant_message_id=102,
        user_text="我更喜欢短一点的回复。",
        assistant_text="知道了，我会尽量简短。",
        candidate_memories=candidates,
    )


def _candidate(memory_id: str) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        kind=MemoryKind.PREFERENCE,
        content="用户偏好简短回复。",
        confidence=0.9,
        salience=0.8,
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        source_message_ids=(90,),
        source_type=MemorySourceType.EXPLICIT_USER,
    )


def test_explicit_user_evidence_must_reference_user_message() -> None:
    provider = FixedProvider(
        """{
          "decision": "CREATE",
          "items": [{
            "kind": "preference",
            "content": "用户偏好简短回复。",
            "confidence": 0.95,
            "salience": 0.8,
            "source_message_ids": [102],
            "evidence": "explicit_user",
            "target_memory_id": null,
            "expires_at": null,
            "tags": [],
            "entities": []
          }],
          "reason_label": "preference"
        }"""
    )

    decision = asyncio.run(MemoryArchivist(provider=provider).analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE


def test_character_inference_cannot_create_preference() -> None:
    provider = FixedProvider(
        """{
          "decision": "CREATE",
          "items": [{
            "kind": "preference",
            "content": "用户似乎偏好简短回复。",
            "confidence": 0.6,
            "salience": 0.5,
            "source_message_ids": [101],
            "evidence": "character_inference",
            "target_memory_id": null,
            "expires_at": null,
            "tags": [],
            "entities": []
          }],
          "reason_label": "inferred_preference"
        }"""
    )

    decision = asyncio.run(MemoryArchivist(provider=provider).analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE


def test_assistant_self_evidence_is_limited_to_self_memory() -> None:
    provider = FixedProvider(
        """{
          "decision": "CREATE",
          "items": [{
            "kind": "fact",
            "content": "用户偏好简短回复。",
            "confidence": 0.9,
            "salience": 0.7,
            "source_message_ids": [102],
            "evidence": "assistant_self",
            "target_memory_id": null,
            "expires_at": null,
            "tags": [],
            "entities": []
          }],
          "reason_label": "assistant_claim"
        }"""
    )

    decision = asyncio.run(MemoryArchivist(provider=provider).analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE


def test_sensitive_metadata_is_rejected_with_harmless_content() -> None:
    provider = FixedProvider(
        """{
          "decision": "CREATE",
          "items": [{
            "kind": "preference",
            "content": "用户偏好简短回复。",
            "confidence": 0.95,
            "salience": 0.8,
            "source_message_ids": [101],
            "evidence": "explicit_user",
            "target_memory_id": null,
            "expires_at": null,
            "tags": ["api_key=secret-value"],
            "entities": []
          }],
          "reason_label": "preference"
        }"""
    )

    decision = asyncio.run(MemoryArchivist(provider=provider).analyze(_context()))

    assert decision.decision is ArchivistAction.NO_WRITE


def test_archivist_cannot_mutate_memory_not_exposed_as_candidate() -> None:
    provider = FixedProvider(
        """{
          "decision": "REJECT",
          "items": [{
            "kind": "preference",
            "content": "用户纠正了旧偏好。",
            "confidence": 0.95,
            "salience": 0.8,
            "source_message_ids": [101],
            "evidence": "explicit_user",
            "target_memory_id": "mem_not_exposed",
            "expires_at": null,
            "tags": [],
            "entities": []
          }],
          "reason_label": "correction"
        }"""
    )

    decision = asyncio.run(
        MemoryArchivist(provider=provider).analyze(
            _context(candidates=(_candidate("mem_visible"),))
        )
    )

    assert decision.decision is ArchivistAction.NO_WRITE
    assert decision.reason_label == "archivist_items_rejected"
