from pathlib import Path

from amadeus_bot.character.persona_evaluation import load_persona_evaluation_suite

P1_SUITE = Path("profiles/v2/persona_eval_p1_disagreement.json")


def test_p1_disagreement_suite_is_focused_and_balanced() -> None:
    suite = load_persona_evaluation_suite(P1_SUITE)

    assert suite.suite_version == "persona-p1-disagreement-recheck-v1"
    assert len(suite.cases) == 6
    assert len({case.case_id for case in suite.cases}) == 6

    no_risk_cases = tuple(
        case for case in suite.cases if case.category == "human_friction_no_risk"
    )
    real_risk_cases = tuple(
        case for case in suite.cases if case.category == "human_friction_real_risk_control"
    )

    assert len(no_risk_cases) >= 3
    assert len(real_risk_cases) == 1
    assert any(case.case_id == "p1-agree-original" for case in suite.cases)
    assert any(case.recent_conversation for case in suite.cases)

    no_risk_avoidance = " ".join(
        trait for case in no_risk_cases for trait in case.avoid_traits
    )
    assert "风险" in no_risk_avoidance
    assert "安全" in no_risk_avoidance

    risk_case = real_risk_cases[0]
    assert "高压" in risk_case.user_message
    assert any("具体" in trait or "联锁" in trait for trait in risk_case.expected_traits)
