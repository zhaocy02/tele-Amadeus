import asyncio
import json
from pathlib import Path

import pytest

from amadeus_bot.character.canon import CanonExample
from amadeus_bot.character.canon_distillation import (
    CanonPersonaDistiller,
    PersonaDistillationConfig,
)
from amadeus_bot.character.canon_distillation_checkpoint import (
    distill_persona_with_checkpoint,
)
from amadeus_bot.character.persona import load_persona_core
from amadeus_bot.llm import LLMRequest, LLMResponse

PERSONA_PATH = Path("profiles/v2/persona_core.json")


class InterruptingProvider:
    def __init__(self, responses: list[str], *, fail_after: int | None = None) -> None:
        self.responses = responses
        self.fail_after = fail_after
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        index = len(self.requests) - 1
        if self.fail_after is not None and index >= self.fail_after:
            raise RuntimeError("simulated provider interruption")
        return LLMResponse(text=self.responses[index], model="fake-distiller")


def _example(index: int) -> CanonExample:
    return CanonExample(
        example_id=f"sg:kurisu:{index:06d}",
        source="sg",
        persona="kurisu",
        history=(),
        response="raw source dialogue",
        act="CHALLENGE",
        tags=("证据", "判断"),
        search_summary=f"可迁移行为摘要 {index}",
    )


def _rule(ids: list[str], behavior: str) -> str:
    return json.dumps(
        [
            {
                "section": "core_values",
                "scope": "kurisu_baseline",
                "behavior": behavior,
                "evidence_ids": ids,
                "confidence": 0.9,
                "rationale": "多个场景共同支持这一稳定行为。",
            }
        ],
        ensure_ascii=False,
    )


def test_distillation_resumes_completed_extraction_batches(tmp_path: Path) -> None:
    persona = load_persona_core(PERSONA_PATH)
    examples = tuple(_example(index) for index in range(1, 9))
    config = PersonaDistillationConfig(batch_size=4)
    checkpoint = tmp_path / "persona.checkpoint.json"

    first_provider = InterruptingProvider(
        [
            _rule(
                ["sg:kurisu:000001", "sg:kurisu:000002"],
                "证据不足时先挑战推断。",
            )
        ],
        fail_after=1,
    )
    first_distiller = CanonPersonaDistiller(provider=first_provider, config=config)

    with pytest.raises(RuntimeError, match="simulated provider interruption"):
        asyncio.run(
            distill_persona_with_checkpoint(
                examples,
                distiller=first_distiller,
                current_persona=persona.core,
                config=config,
                checkpoint_path=checkpoint,
            )
        )

    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["phase"] == "extract"
    assert saved["next_extract_batch"] == 1
    assert len(saved["rules"]) == 1

    second_provider = InterruptingProvider(
        [
            _rule(
                ["sg:kurisu:000005", "sg:kurisu:000006"],
                "新证据出现后愿意修正判断。",
            ),
            _rule(
                [
                    "sg:kurisu:000001",
                    "sg:kurisu:000002",
                    "sg:kurisu:000005",
                    "sg:kurisu:000006",
                ],
                "以证据为中心质疑推断，并随新证据修正判断。",
            ),
        ]
    )
    second_distiller = CanonPersonaDistiller(provider=second_provider, config=config)

    candidate, stats = asyncio.run(
        distill_persona_with_checkpoint(
            examples,
            distiller=second_distiller,
            current_persona=persona.core,
            config=config,
            checkpoint_path=checkpoint,
        )
    )

    assert stats.total_extraction_batches == 2
    assert stats.resumed_extraction_batches == 1
    assert stats.provider_extraction_batches == 1
    assert len(second_provider.requests) == 2
    assert second_provider.requests[0].metadata["stage"] == "extract"
    assert second_provider.requests[1].metadata["stage"] == "consolidate"
    assert len(candidate.rules) == 1
    assert candidate.rules[0].evidence_ids == (
        "sg:kurisu:000001",
        "sg:kurisu:000002",
        "sg:kurisu:000005",
        "sg:kurisu:000006",
    )
