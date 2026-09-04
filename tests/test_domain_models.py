from datetime import UTC, datetime

import pytest

from amadeus_bot.character import CharacterStateSnapshot
from amadeus_bot.memory import MemoryKind, MemoryRecord


def test_memory_requires_confidence_and_salience_ranges() -> None:
    with pytest.raises(ValueError, match="confidence"):
        MemoryRecord(
            memory_id="m1",
            kind=MemoryKind.FACT,
            content="example",
            confidence=1.1,
            salience=0.5,
            created_at=datetime.now(UTC),
            source_message_ids=(1,),
        )


def test_character_state_snapshot_is_versioned_and_immutable() -> None:
    values = {"relationship_tone": "baseline"}
    snapshot = CharacterStateSnapshot(
        version=1,
        updated_at=datetime.now(UTC),
        values=values,
    )
    values["relationship_tone"] = "changed outside"

    assert snapshot.values["relationship_tone"] == "baseline"
    with pytest.raises(TypeError):
        snapshot.values["relationship_tone"] = "forbidden"  # type: ignore[index]
