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
    load_persona_distillation_candidate,
    load_persona_evaluation_suite,
    overlay_persona_candidate,
    write_persona_evaluation_report,
)
from amadeus_bot.llm import LLMRequest, LLMResponse, MessageRole

PERSONA_PATH = Path("profiles/v2/persona_core.json")


class InspectingProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        developer = request.messages[0].content
        candidate = "证据改变时要明显更新判断" in developer
        canon = "面对实验结论时先检查证据充分性" in developer
        return LLMResponse(
            text=f"candidate={candidate};canon={canon}",
            model="fake-eval",
        )


class PairedPolicyProvider:
    def __init__(self) -> None:
        self.policy_requests: list[LLMRequest] = []
        self.generation_requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if request.messages[0].role is MessageRole.SYSTEM:
            self.policy_requests.append(request)
            return LLMResponse(
                text=json.dumps(
                    {
                        "act": "TEASE",
                        "intensity": 0.6,
                        "answer_obligation": "minimal",
                        "memory_callback_ids": [],
                        "state_bias": "guarded",
                        "reason_label": "paired_intimacy_test",
                    }
                ),
                model="fake-eval",
            )
        self.generation_requests.append(request)
        developer = request.messages[0].content
        canon = "嘴硬方式回应在意" in developer
        return LLMResponse(text=f"canon={canon}", model="fake-eval")


def _suite() -> PersonaEvaluationSuite:
    return PersonaEvaluationSuite(
        suite_version="test-v1",
        cases=(
            PersonaEvaluationCase(
                case_id="science",
                category="scientific_reasoning",
                user_message="这个实验数据不足，可以下结论吗？",
                expected_traits=("检查证据",),
                avoid_traits=("盲目同意",),
            ),
        ),
    )


def _candidate() -> PersonaDistillationCandidate:
    return PersonaDistillationCandidate(
        source_example_count=2,
        current_persona_version="kurisu-v2.1.0",
        rules=(
            DistilledPersonaRule(
                section="core_values",
                scope="kurisu_baseline",
                behavior="证据改变时要明显更新判断。",
                evidence_ids=("sg:1", "sg:2"),
                confidence=0.9,
                rationale="两个独立场景支持这个模式。",
            ),
        ),
    )


def _canon() -> tuple[CanonExample, ...]:
    return (
        CanonExample(
            example_id="sg:science:1",
            source="sg",
            persona="kurisu",
            history=(),
            response="raw",
            act="DIRECT_ANSWER",
            tags=("实验", "数据", "结论"),
            search_summary="面对实验结论时先检查证据充分性，再决定是否接受推断。",
        ),
    )


def test_runner_produces_four_inspectable_variants() -> None:
    provider = InspectingProvider()
    persona = load_persona_core(PERSONA_PATH)
    runner = PersonaEvaluationRunner(
        provider=provider,
        base_persona=persona,
        suite=_suite(),
        candidate=_candidate(),
        canon_examples=_canon(),
    )

    report = asyncio.run(runner.run())

    assert report.schema_version == 2
    assert [item.variant for item in report.observations] == [
        "baseline_rag_off",
        "baseline_rag_on",
        "candidate_rag_off",
        "candidate_rag_on",
    ]
    responses = {item.variant: item.response for item in report.observations}
    assert responses["baseline_rag_off"] == "candidate=False;canon=False"
    assert responses["baseline_rag_on"] == "candidate=False;canon=True"
    assert responses["candidate_rag_off"] == "candidate=True;canon=False"
    assert responses["candidate_rag_on"] == "candidate=True;canon=True"
    observations = {item.variant: item for item in report.observations}
    assert observations["baseline_rag_off"].canon_example_ids == ()
    assert observations["baseline_rag_on"].canon_example_ids == ("sg:science:1",)
    assert observations["candidate_rag_on"].persona_version.startswith("kurisu-v2.1.0+eval.")
    assert report.candidate_version == "canon-distillation-v2-kurisu-primary"
    assert report.canon_example_count == 1


def test_rag_off_on_reuses_one_deliberate_policy_plan() -> None:
    provider = PairedPolicyProvider()
    persona = load_persona_core(PERSONA_PATH)
    suite = PersonaEvaluationSuite(
        suite_version="paired-policy-v1",
        cases=(
            PersonaEvaluationCase(
                case_id="intimacy",
                category="relationship",
                user_message="你是不是其实有点想我？",
                expected_traits=("保持个人立场",),
            ),
        ),
    )
    canon = (
        CanonExample(
            example_id="sg:kurisu:intimacy",
            source="sg",
            persona="kurisu",
            history=(),
            response="raw",
            act="TEASE",
            tags=("想我", "嘴硬", "关系"),
            search_summary="被问你是不是其实有点想我时，她以嘴硬方式回应在意。",
        ),
    )
    runner = PersonaEvaluationRunner(
        provider=provider,
        base_persona=persona,
        suite=suite,
        model="fake-eval",
        canon_examples=canon,
    )

    report = asyncio.run(runner.run())
    off, on = report.observations

    assert len(provider.policy_requests) == 1
    assert len(provider.generation_requests) == 2
    assert off.variant == "baseline_rag_off"
    assert on.variant == "baseline_rag_on"
    assert off.policy_act == on.policy_act == "TEASE"
    assert off.policy_reason_label == on.policy_reason_label == "paired_intimacy_test"
    assert off.policy_answer_obligation == on.policy_answer_obligation == "minimal"
    assert off.policy_intensity == on.policy_intensity == 0.6
    assert off.policy_state_bias == on.policy_state_bias == "guarded"
    assert off.policy_fingerprint == on.policy_fingerprint
    assert off.policy_ms == on.policy_ms
    assert off.canon_example_ids == ()
    assert on.canon_example_ids == ("sg:kurisu:intimacy",)
    assert off.response == "canon=False"
    assert on.response == "canon=True"
    assert off.context_ms >= 0
    assert on.context_ms >= 0


def test_candidate_overlay_is_in_memory_and_keeps_protected_sections() -> None:
    base = load_persona_core(PERSONA_PATH)
    candidate = _candidate()

    overlaid = overlay_persona_candidate(base, candidate)

    assert "证据改变时要明显更新判断。" not in base.core.core_values
    assert "证据改变时要明显更新判断。" in overlaid.core.core_values
    assert overlaid.core.fact_boundary == base.core.fact_boundary
    assert overlaid.core.triggers == base.core.triggers
    assert overlaid.version_hash != base.version_hash
    assert overlaid.source_path == base.source_path


def test_suite_and_candidate_loaders_validate_json(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(_suite().model_dump(mode="json")), encoding="utf-8")
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(
        json.dumps(_candidate().model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    suite = load_persona_evaluation_suite(suite_path)
    candidate = load_persona_distillation_candidate(candidate_path)

    assert suite.suite_version == "test-v1"
    assert candidate.rules[0].section == "core_values"


def test_suite_rejects_duplicate_case_ids() -> None:
    case = PersonaEvaluationCase(
        case_id="duplicate",
        category="test",
        user_message="测试",
        expected_traits=("自然",),
    )
    with pytest.raises(ValueError, match="unique"):
        PersonaEvaluationSuite(suite_version="test", cases=(case, case))


def test_report_writer_preserves_cases_and_observations(tmp_path: Path) -> None:
    provider = InspectingProvider()
    report = asyncio.run(
        PersonaEvaluationRunner(
            provider=provider,
            base_persona=load_persona_core(PERSONA_PATH),
            suite=_suite(),
        ).run()
    )
    destination = tmp_path / "report.json"

    write_persona_evaluation_report(destination, report)

    raw = json.loads(destination.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert raw["suite_version"] == "test-v1"
    assert raw["cases"][0]["expected_traits"] == ["检查证据"]
    assert raw["observations"][0]["variant"] == "baseline_rag_off"
    assert "policy_fingerprint" in raw["observations"][0]
    assert "context_ms" in raw["observations"][0]
