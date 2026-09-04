from pathlib import Path

import pytest

from amadeus_bot.character.legacy_persona import (
    LEGACY_PERSONA_FILES,
    PersonaLoadError,
    load_legacy_persona,
)


def _write_persona(directory: Path) -> None:
    directory.mkdir()
    for index, filename in enumerate(LEGACY_PERSONA_FILES):
        (directory / filename).write_text(f"section-{index}\n", encoding="utf-8")


def test_legacy_persona_loads_in_fixed_order_and_has_stable_hash(tmp_path: Path) -> None:
    directory = tmp_path / "persona"
    _write_persona(directory)

    first = load_legacy_persona(directory)
    second = load_legacy_persona(directory)

    assert first.instructions.split("\n\n") == [f"section-{i}" for i in range(5)]
    assert first.version_hash == second.version_hash
    assert first.source_files == LEGACY_PERSONA_FILES


def test_legacy_persona_rejects_missing_source_file(tmp_path: Path) -> None:
    directory = tmp_path / "persona"
    _write_persona(directory)
    (directory / "STYLE.md").unlink()

    with pytest.raises(PersonaLoadError, match="STYLE.md"):
        load_legacy_persona(directory)
