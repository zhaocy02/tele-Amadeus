import asyncio
import json
from pathlib import Path

import pytest

from amadeus_bot.character.canon import CanonExample
from amadeus_bot.character.canon_distillation import (
    DistilledPersonaRule,
    PersonaDistillationCandidate,
)
from amadeus_bot.character.persona import load_persona_core
from amadeus_bot.character.persona_evaluation import (
    PersonaEvaluationCase,
    PersonaEvaluationRunner,
    PersonaEvaluationSuite,
)
from amadeus_bot.character.persona_evaluation_checkpoint import (
    run_persona_evaluation_with_checkpoint,
)
from amadeus_bot.llm import LLMRequest, LLMResponse, MessageRole

PERSONA_PATH = Path("profiles/v2/persona_core.json")


class FailingProvider:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if self.fail_after is not None and len(self.requests) >= self.fail_after:
            raise RuntimeError("simulated provider interruption")
        self.requests.append(request)
        if request.messages[0].role is MessageRole.SYSTEM:
            return LLMResponse(
                text=json.dumps(
                    {
                        "act": "TEASE",
                        "intensity": 0.55,
                        "answer_obligation": "minimal",
                        "memory_callback_ids": [],
                        "state_bias": "guarded",
                        "reason_label": "checkpoint_pair_policy",
                    }
                ),
                model="fake-eval",
            )
        return LLMResponse(text=f"reply-{len(self.requests)}", model="fake-eval")


def _suite() -> PersonaEvaluationSuite:
    return PersonaEvaluationSuite(
        suite_version="checkpoint-test-v2",
        cases=(
            PersonaEvaluationCase(
                case_id="intimacy",
                category="relationship",
                user_message="你是不是其实有点想我？",
                expected_traits=("保持个人立场",),
            ),
        ),
    )


def _candidate() -> PersonaDistillationCandidate:
    return PersonaDistillationCandidate(
        source_example_count=2,
        current_persona_version="kurisu-v2.1.0",
        rules=(
            DistilledPersonaRule(
                section="social_behavior",
                scope="kurisu_baseline",
                behavior="普通问候不必自动转成任务。",
                evidence_ids=("sg:1", "sg:2"),
                confidence=0.8,
                rationale="测试候选人格变体。",
            ),
        ),
    )


def _canon() -> tuple[CanonExample, ...]:
    return (
        CanonExample(
            example_id="sg:kurisu:test",
            source="sg",
            persona="kurisu",
            history=(),
            response="raw source dialogue",
            act="TEASE",
            tags=("想我", "关系"),
            search_summary="被问你是不是其实有点想我时，她用带个人立场的方式回应。",
        ),
    )


def _runner(provider: FailingProvider) -> PersonaEvaluationRunner:
    return PersonaEvaluationRunner(
        provider=provider,
        base_persona=load_persona_core(PERSONA_PATH),
        suite=_suite(),
        model="fake-eval",
        candidate=_candidate(),
        canon_examples=_canon(),
    )


def test_evaluation_resumes_with_the_same_paired_policy(tmp_path: Path) -> None:
    checkpoint = tmp_path / "evaluation.checkpoint.json"
    interrupted_provider = FailingProvider(fail_after=2)
    first_progress: list[tuple[int, int, str, str]] = []

    with pytest.raises(RuntimeError, match="simulated provider interruption"):
        asyncio.run(
            run_persona_evaluation_with_checkpoint(
                _runner(interrupted_provider),
                checkpoint_path=checkpoint,
                progress=lambda completed, total, variant, case_id: first_progress.append(
                    (completed, total, variant, case_id)
                ),
            )
        )

    assert checkpoint.exists()
    assert len(interrupted_provider.requests) == 2
    assert interrupted_provider.requests[0].messages[0].role is MessageRole.SYSTEM
    assert interrupted_provider.requests[1].messages[0].role is MessageRole.DEVELOPER
    assert [(item[0], item[1]) for item in first_progress] == [(1, 4)]

    raw_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert raw_checkpoint["version"] == 2
    assert [(item["pair_id"], item["case_id"]) for item in raw_checkpoint["policies"]] == [
        ("baseline", "intimacy")
    ]
    assert [item["variant"] for item in raw_checkpoint["observations"]] == [
        "baseline_rag_off"
    ]

    resumed_provider = FailingProvider()
    resumed_progress: list[tuple[int, int, str, str]] = []
    report, stats = asyncio.run(
        run_persona_evaluation_with_checkpoint(
            _runner(resumed_provider),
            checkpoint_path=checkpoint,
            progress=lambda completed, total, variant, case_id: resumed_progress.append(
                (completed, total, variant, case_id)
            ),
        )
    )

    assert stats.total_observations == 4
    assert stats.resumed_observations == 1
    assert stats.provider_observations == 3
    assert len(resumed_provider.requests) == 4
    assert resumed_provider.requests[0].messages[0].role is MessageRole.DEVELOPER
    assert resumed_provider.requests[1].messages[0].role is MessageRole.SYSTEM
    assert [(item[0], item[1]) for item in resumed_progress] == [
        (2, 4),
        (3, 4),
        (4, 4),
    ]
    assert [item.variant for item in report.observations] == [
        "baseline_rag_off",
        "baseline_rag_on",
        "candidate_rag_off",
        "candidate_rag_on",
    ]
    observations = {item.variant: item for item in report.observations}
    assert (
        observations["baseline_rag_off"].policy_fingerprint
        == observations["baseline_rag_on"].policy_fingerprint
    )
    assert (
        observations["candidate_rag_off"].policy_fingerprint
        == observations["candidate_rag_on"].policy_fingerprint
    )


def test_evaluation_checkpoint_rejects_changed_model(tmp_path: Path) -> None:
    checkpoint = tmp_path / "evaluation.checkpoint.json"
    first_provider = FailingProvider()
    asyncio.run(
        run_persona_evaluation_with_checkpoint(
            _runner(first_provider),
            checkpoint_path=checkpoint,
        )
    )

    second_provider = FailingProvider()
    changed_runner = PersonaEvaluationRunner(
        provider=second_provider,
        base_persona=load_persona_core(PERSONA_PATH),
        suite=_suite(),
        model="different-model",
        candidate=_candidate(),
        canon_examples=_canon(),
    )
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            run_persona_evaluation_with_checkpoint(
                changed_runner,
                checkpoint_path=checkpoint,
            )
        )
