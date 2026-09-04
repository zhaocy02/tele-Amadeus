from pathlib import Path

from amadeus_bot.character.persona_evaluation import load_persona_distillation_candidate

CANDIDATE = Path("profiles/v2/persona_candidate_reviewed.json")


def test_reviewed_candidate_r3_preserves_eight_rules_and_narrows_risk_scope() -> None:
    candidate = load_persona_distillation_candidate(CANDIDATE)

    assert candidate.candidate_version == "canon-distillation-v2-reviewed-kurisu-primary-r3"
    assert candidate.source_example_count == 922
    assert len(candidate.rules) == 8

    risk_rules = tuple(
        rule
        for rule in candidate.rules
        if rule.section == "core_values" and "危险" in rule.behavior
    )
    assert len(risk_rules) == 1
    risk = risk_rules[0].behavior
    assert "当前上下文明确给出" in risk
    assert "普通意见、审美、关系摩擦或低风险选择不主动补造危险前提" in risk
    assert "一般证据不足只按证据问题处理" in risk


def test_reviewed_candidate_r3_social_escalation_uses_concrete_boundary_wording() -> None:
    candidate = load_persona_distillation_candidate(CANDIDATE)
    social = tuple(rule for rule in candidate.rules if rule.section == "social_behavior")

    assert len(social) == 1
    assert "明确侵犯边界" in social[0].behavior
    assert "危险越界" not in social[0].behavior
