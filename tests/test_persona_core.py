from pathlib import Path

import pytest

from amadeus_bot.character import PersonaCoreLoadError, load_persona_core

PERSONA_PATH = Path("profiles/v2/persona_core.json")


def test_load_persona_core_is_versioned_and_deterministic() -> None:
    first = load_persona_core(PERSONA_PATH)
    second = load_persona_core(PERSONA_PATH)

    assert first.core.persona_version == "kurisu-v2.1.0"
    assert first.core.schema_version == 1
    assert first.version_hash == second.version_hash
    assert len(first.version_hash) == 64
    assert first.source_path.name == "persona_core.json"


def test_character_prompt_preserves_explicit_boundaries() -> None:
    loaded = load_persona_core(PERSONA_PATH)
    prompt = loaded.core.render_character_prompt()

    assert "[IDENTITY]" in prompt
    assert "[CORE VALUES]" in prompt
    assert "[TRIGGERS]" in prompt
    assert "[ANTI-ASSISTANT]" in prompt
    assert "[FACT BOUNDARY]" in prompt
    assert "Christina" in prompt


def test_policy_context_excludes_fact_boundary_and_full_identity_dump() -> None:
    loaded = load_persona_core(PERSONA_PATH)
    context = loaded.core.render_policy_context()

    assert "[CORE VALUES]" in context
    assert "[SOCIAL BEHAVIOR]" in context
    assert "[TRIGGERS]" in context
    assert "[FACT BOUNDARY]" not in context
    assert "[IDENTITY]" not in context


def test_invalid_persona_core_fails_closed(tmp_path: Path) -> None:
    invalid = tmp_path / "persona.json"
    invalid.write_text('{"schema_version": 1, "persona_version": "x"}', encoding="utf-8")

    with pytest.raises(PersonaCoreLoadError):
        load_persona_core(invalid)
