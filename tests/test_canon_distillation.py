import asyncio
import json
from pathlib import Path

import pytest

from amadeus_bot.character.canon import CanonExample
from amadeus_bot.character.canon_distillation import (
    CANON_DISTILLATION_PROMPT_VERSION,
    CanonPersonaDistiller,
    PersonaDistillationCandidate,
    PersonaDistillationConfig,
    write_persona_distillation_candidate,
)
from amadeus_bot.character.persona import load_persona_core
from amadeus_bot.llm import LLMRequest, LLMResponse

PERSONA_PATH = Path("profiles/v2/persona_core.json")


class SequenceProvider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = len(self.requests) - 1
        return LLMResponse(text=self.responses[index], model="fake-distiller")


def _example(
    example_id: str,
    summary: str,
    *,
    source: str = "sg",
    persona: str = "kurisu",
) -> CanonExample:
    return CanonExample(
        example_id=example_id,
        source=source,
        persona=persona,
        history=(),
        response="raw source dialogue",
        act="CHALLENGE",
        tags=("证据", "判断"),
        search_summary=summary,
    )


def test_distiller_extracts_evidence_backed_candidate_without_mutating_persona() -> None:
    provider = SequenceProvider(
        [
            json.dumps(
                [
                    {
                        "section": "core_values",
                        "scope": "kurisu_baseline",
                        "behavior": "证据不足时先挑战推断，证据增强后愿意修正判断。",
                        "evidence_ids": ["sg:1", "sg:2"],
                        "confidence": 0.91,
                        "rationale": "两个科学讨论场景都表现出先质疑、后随证据更新。",
                    }
                ],
                ensure_ascii=False,
            )
        ]
    )
    persona = load_persona_core(PERSONA_PATH)
    examples = (
        _example("sg:1", "面对证据不足的结论时先指出推断过度。"),
        _example("sg:2", "新证据出现后会重新评估而不是维护原判断。"),
    )
    distiller = CanonPersonaDistiller(
        provider=provider,
        config=PersonaDistillationConfig(batch_size=4),
    )

    candidate = asyncio.run(distiller.distill(examples, current_persona=persona.core))

    assert candidate.current_persona_version == persona.core.persona_version
    assert candidate.source_example_count == 2
    assert len(candidate.rules) == 1
    assert candidate.rules[0].evidence_ids == ("sg:1", "sg:2")
    assert candidate.rules[0].scope == "kurisu_baseline"
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.metadata == {
        "prompt_version": CANON_DISTILLATION_PROMPT_VERSION,
        "stage": "extract",
    }
    developer = request.messages[0].content
    assert persona.core.persona_version in developer
    assert "tsundere" in developer
    assert "primary personality target is canonical Makise Kurisu" in developer
    assert "generic AI-assistant norms" in developer
    assert "more perfect assistant than Kurisu" in developer


def test_distiller_consolidates_provisional_rules() -> None:
    provider = SequenceProvider(
        [
            json.dumps(
                [
                    {
                        "section": "social_behavior",
                        "scope": "kurisu_baseline",
                        "behavior": "亲近关系中的关心常先表现为纠正或轻微责备。",
                        "evidence_ids": ["sg:1", "sg:2"],
                        "confidence": 0.82,
                        "rationale": "两段场景均以实际干预而非直接安慰表达关心。",
                    },
                    {
                        "section": "social_behavior",
                        "scope": "kurisu_baseline",
                        "behavior": "担心对方时仍倾向给出实际建议而不是泛化安慰。",
                        "evidence_ids": ["sg:3", "sg:4"],
                        "confidence": 0.85,
                        "rationale": "关心通过具体建议表达。",
                    },
                ],
                ensure_ascii=False,
            ),
            json.dumps(
                [
                    {
                        "section": "social_behavior",
                        "scope": "kurisu_baseline",
                        "behavior": "关心亲近的人时优先以纠正、提醒或实际建议介入。",
                        "evidence_ids": ["sg:1", "sg:2", "sg:3", "sg:4"],
                        "confidence": 0.9,
                        "rationale": "四段独立场景共同支持行动导向的关心方式。",
                    }
                ],
                ensure_ascii=False,
            ),
        ]
    )
    persona = load_persona_core(PERSONA_PATH)
    examples = tuple(
        _example(f"sg:{index}", f"场景 {index} 的可迁移行为摘要") for index in range(1, 5)
    )
    distiller = CanonPersonaDistiller(
        provider=provider,
        config=PersonaDistillationConfig(batch_size=4),
    )

    candidate = asyncio.run(distiller.distill(examples, current_persona=persona.core))

    assert len(provider.requests) == 2
    assert provider.requests[1].metadata["stage"] == "consolidate"
    assert "Kurisu is the personality baseline" in provider.requests[1].messages[0].content
    assert "good AI assistant" in provider.requests[1].messages[0].content
    assert len(candidate.rules) == 1
    assert candidate.rules[0].evidence_ids == ("sg:1", "sg:2", "sg:3", "sg:4")


def test_amadeus_delta_requires_amadeus_evidence() -> None:
    provider = SequenceProvider(
        [
            json.dumps(
                [
                    {
                        "section": "identity",
                        "scope": "amadeus_delta",
                        "behavior": "数字身份下会明确区分自身记忆边界。",
                        "evidence_ids": ["sg:1", "sg:2"],
                        "confidence": 0.8,
                        "rationale": "测试非法 scope evidence。",
                    }
                ],
                ensure_ascii=False,
            )
        ]
    )
    persona = load_persona_core(PERSONA_PATH)
    distiller = CanonPersonaDistiller(
        provider=provider,
        config=PersonaDistillationConfig(batch_size=4),
    )

    with pytest.raises(ValueError, match="Amadeus evidence"):
        asyncio.run(
            distiller.distill(
                (
                    _example("sg:1", "摘要一"),
                    _example("sg:2", "摘要二"),
                ),
                current_persona=persona.core,
            )
        )


def test_unenriched_corpus_returns_empty_candidate_without_provider_call() -> None:
    provider = SequenceProvider([])
    persona = load_persona_core(PERSONA_PATH)
    distiller = CanonPersonaDistiller(provider=provider)
    examples = (
        CanonExample(
            example_id="sg:raw:1",
            source="sg",
            persona="kurisu",
            history=(),
            response="raw",
        ),
    )

    candidate = asyncio.run(distiller.distill(examples, current_persona=persona.core))

    assert candidate.rules == ()
    assert candidate.source_example_count == 0
    assert provider.requests == []


def test_candidate_writer_uses_separate_review_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "candidate.json"
    candidate = PersonaDistillationCandidate(
        source_example_count=4,
        current_persona_version="kurisu-v2.0.0",
        rules=(),
    )

    write_persona_distillation_candidate(destination, candidate)

    written = json.loads(destination.read_text(encoding="utf-8"))
    assert written["candidate_version"] == "canon-distillation-v2-kurisu-primary"
    assert written["current_persona_version"] == "kurisu-v2.0.0"
    assert written["rules"] == []
