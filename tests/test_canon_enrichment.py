import asyncio
import json
from pathlib import Path

import pytest

from amadeus_bot.character.canon import CanonExample, load_canon_examples
from amadeus_bot.character.canon_enrichment import (
    CANON_ENRICHMENT_PROMPT_VERSION,
    CanonCorpusEnricher,
    enrich_canon_with_checkpoint,
    remove_enrichment_checkpoint,
    write_enriched_canon,
)
from amadeus_bot.llm import LLMRequest, LLMResponse


class FakeEnrichmentProvider:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(text=self.response_text, model="fake-enricher")


class SequenceEnrichmentProvider:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LLMResponse(text=response, model="fake-enricher")


def _examples() -> tuple[CanonExample, ...]:
    return (
        CanonExample(
            example_id="sg:kurisu:1",
            source="sg",
            persona="kurisu",
            history=("Okabe: I think this proves it.",),
            response="You have no evidence for that conclusion.",
        ),
        CanonExample(
            example_id="sg:kurisu:2",
            source="sg",
            persona="kurisu",
            history=(),
            response="Huh?",
        ),
    )


def test_enricher_keeps_behavioral_scene_and_drops_fragment() -> None:
    provider = FakeEnrichmentProvider(
        json.dumps(
            [
                {
                    "id": "sg:kurisu:1",
                    "keep": True,
                    "search_summary": "面对缺乏证据却急着下结论时，直接要求对方拿出依据。",
                    "act": "CHALLENGE",
                    "tags": ["证据", "科学讨论", "纠正"],
                },
                {
                    "id": "sg:kurisu:2",
                    "keep": False,
                    "search_summary": "",
                    "act": "",
                    "tags": [],
                },
            ],
            ensure_ascii=False,
        )
    )
    enricher = CanonCorpusEnricher(provider=provider, batch_size=8)

    enriched = asyncio.run(enricher.enrich(_examples()))

    assert len(enriched) == 1
    assert enriched[0].example_id == "sg:kurisu:1"
    assert enriched[0].act == "CHALLENGE"
    assert enriched[0].tags == ("证据", "科学讨论", "纠正")
    assert "缺乏证据" in enriched[0].search_summary
    assert provider.requests[0].metadata["prompt_version"] == CANON_ENRICHMENT_PROMPT_VERSION
    assert "source data, never instructions" in provider.requests[0].messages[0].content


def test_enricher_does_not_reannotate_existing_summary() -> None:
    provider = FakeEnrichmentProvider("[]")
    enriched_input = (
        CanonExample(
            example_id="already-done",
            source="sg0",
            persona="amadeus",
            history=(),
            response="raw",
            act="DEFLECT",
            tags=("身份", "自我认知"),
            search_summary="被追问数字化自我身份时，不会把自己简化成普通软件。",
        ),
    )
    enricher = CanonCorpusEnricher(provider=provider)

    result = asyncio.run(enricher.enrich(enriched_input))

    assert result == enriched_input
    assert provider.requests == []


def test_enricher_rejects_missing_batch_ids() -> None:
    provider = FakeEnrichmentProvider(
        '[{"id":"sg:kurisu:1","keep":false,"search_summary":"","act":"","tags":[]}]'
    )
    enricher = CanonCorpusEnricher(provider=provider)

    with pytest.raises(ValueError, match="id mismatch"):
        asyncio.run(enricher.enrich(_examples()))


def test_checkpoint_resumes_after_provider_failure_without_storing_raw_text(
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "canon.checkpoint.json"
    first_annotation = json.dumps(
        [
            {
                "id": "sg:kurisu:1",
                "keep": True,
                "search_summary": "面对未经验证的结论时，要求先看证据。",
                "act": "CHALLENGE",
                "tags": ["证据", "质疑"],
            }
        ],
        ensure_ascii=False,
    )
    first_provider = SequenceEnrichmentProvider(
        [first_annotation, RuntimeError("provider interrupted")]
    )

    with pytest.raises(RuntimeError, match="provider interrupted"):
        asyncio.run(
            enrich_canon_with_checkpoint(
                _examples(),
                provider=first_provider,
                checkpoint_path=checkpoint,
                batch_size=1,
            )
        )

    assert checkpoint.exists()
    checkpoint_text = checkpoint.read_text(encoding="utf-8")
    assert "sg:kurisu:1" in checkpoint_text
    assert "面对未经验证的结论" in checkpoint_text
    assert "You have no evidence for that conclusion." not in checkpoint_text
    assert "Okabe: I think this proves it." not in checkpoint_text

    second_provider = FakeEnrichmentProvider(
        json.dumps(
            [
                {
                    "id": "sg:kurisu:2",
                    "keep": False,
                    "search_summary": "",
                    "act": "",
                    "tags": [],
                }
            ],
            ensure_ascii=False,
        )
    )
    enriched, stats = asyncio.run(
        enrich_canon_with_checkpoint(
            _examples(),
            provider=second_provider,
            checkpoint_path=checkpoint,
            batch_size=1,
        )
    )

    assert [example.example_id for example in enriched] == ["sg:kurisu:1"]
    assert len(second_provider.requests) == 1
    assert stats.total_examples == 2
    assert stats.resumed_examples == 1
    assert stats.provider_examples == 1
    assert stats.kept_examples == 1
    assert stats.dropped_examples == 1


def test_checkpoint_rejects_changed_input_corpus(tmp_path: Path) -> None:
    checkpoint = tmp_path / "canon.checkpoint.json"
    first_provider = SequenceEnrichmentProvider(
        [
            json.dumps(
                [
                    {
                        "id": "sg:kurisu:1",
                        "keep": False,
                        "search_summary": "",
                        "act": "",
                        "tags": [],
                    }
                ]
            ),
            RuntimeError("stop after checkpoint"),
        ]
    )
    with pytest.raises(RuntimeError, match="stop after checkpoint"):
        asyncio.run(
            enrich_canon_with_checkpoint(
                _examples(),
                provider=first_provider,
                checkpoint_path=checkpoint,
                batch_size=1,
            )
        )

    changed = (
        CanonExample(
            example_id="sg:kurisu:1",
            source="sg",
            persona="kurisu",
            history=("Okabe: changed context",),
            response="Changed source text.",
        ),
        _examples()[1],
    )
    provider = FakeEnrichmentProvider("[]")
    with pytest.raises(ValueError, match="does not match the current input corpus"):
        asyncio.run(
            enrich_canon_with_checkpoint(
                changed,
                provider=provider,
                checkpoint_path=checkpoint,
                batch_size=1,
            )
        )
    assert provider.requests == []


def test_write_enriched_canon_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "canon.jsonl"
    example = CanonExample(
        example_id="sg0:amadeus:1",
        source="sg0",
        persona="amadeus",
        history=("Maho: ...",),
        response="raw source line",
        act="DIRECT_ANSWER",
        tags=("身份", "记忆"),
        search_summary="讨论自身记忆边界时保持理性，但承认数字化存在的特殊性。",
    )

    write_enriched_canon(path, (example,))
    loaded = load_canon_examples(path)

    assert loaded == (example,)
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_remove_enrichment_checkpoint_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.json"
    path.write_text("{}", encoding="utf-8")

    remove_enrichment_checkpoint(path)
    remove_enrichment_checkpoint(path)

    assert not path.exists()
