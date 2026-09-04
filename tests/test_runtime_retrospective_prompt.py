from amadeus_bot.character.runtime_retrospective import SchemaGuidedCharacterRetrospective


def test_runtime_retrospective_prompt_exposes_strict_schema() -> None:
    prompt = SchemaGuidedCharacterRetrospective._system_prompt()

    assert '"decision":"NO_CHANGE|UPDATE"' in prompt
    assert '"status":"active|weakened|rejected"' in prompt
    assert '"scope":"casual_only|general"' in prompt
    assert "Return exactly one JSON object and no markdown or commentary." in prompt
    assert "Never invent source IDs" in prompt
