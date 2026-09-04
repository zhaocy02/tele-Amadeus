from pathlib import Path

import pytest

from amadeus_bot.character.persona import load_persona_core
from amadeus_bot.character.persona_evaluation import (
    PersonaEvaluationCase,
    PersonaEvaluationRunner,
    PersonaEvaluationSuite,
)

BASE = Path("profiles/v2/persona_core.json")
CANDIDATE = Path("profiles/v2/persona_core_candidate.json")


class _UnusedProvider:
    async def generate(self, request: object) -> object:
        raise AssertionError(f"provider should not be called: {request}")


def _suite() -> PersonaEvaluationSuite:
    return PersonaEvaluationSuite(
        suite_version="exact-persona-test-v1",
        cases=(
            PersonaEvaluationCase(
                case_id="test",
                category="test",
                user_message="测试",
                expected_traits=("自然",),
            ),
        ),
    )


def test_persona_core_v21_matches_validated_rc1_except_version() -> None:
    base = load_persona_core(BASE)
    candidate = load_persona_core(CANDIDATE)

    assert base.core.persona_version == "kurisu-v2.1.0"
    assert candidate.core.persona_version == "kurisu-v2.1.0-rc1"

    base_payload = base.core.model_dump()
    candidate_payload = candidate.core.model_dump()
    base_payload["persona_version"] = candidate_payload["persona_version"]
    assert base_payload == candidate_payload

    core_values = "\n".join(base.core.core_values)
    assert "普通意见、审美、关系摩擦、低风险选择或一般证据不足不主动补造危险前提" in core_values
    assert "只有存在具体风险事实时才把指出风险作为关心的一部分" in core_values

    relationship = "\n".join(base.core.relationship_baseline)
    assert "阻止已经明确的涉险行为" in relationship
    assert "不会凭 canon 补造共同经历" in relationship


def test_runner_uses_exact_candidate_persona_without_additive_overlay() -> None:
    base = load_persona_core(BASE)
    candidate = load_persona_core(CANDIDATE)
    runner = PersonaEvaluationRunner(
        provider=_UnusedProvider(),  # type: ignore[arg-type]
        base_persona=base,
        candidate_persona=candidate,
        suite=_suite(),
    )

    variants = runner._variants()

    assert runner._candidate_version() == "kurisu-v2.1.0-rc1"
    assert [item[0] for item in variants] == ["baseline_rag_off", "candidate_rag_off"]
    assert variants[1][1].source_path == candidate.source_path
    assert variants[1][1].version_hash == candidate.version_hash


def test_runner_rejects_overlay_and_exact_candidate_together() -> None:
    base = load_persona_core(BASE)
    candidate = load_persona_core(CANDIDATE)
    with pytest.raises(ValueError, match="mutually exclusive"):
        PersonaEvaluationRunner(
            provider=_UnusedProvider(),  # type: ignore[arg-type]
            base_persona=base,
            candidate_persona=candidate,
            candidate=object(),  # type: ignore[arg-type]
            suite=_suite(),
        )
