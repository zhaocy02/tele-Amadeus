from datetime import UTC, datetime, timedelta

import pytest

from amadeus_bot.character import (
    AssumptionStatus,
    CharacterState,
    MistakeScope,
    PlausibleMistake,
    StateResidue,
    WorkingAssumption,
)


def _at(hour: int = 12) -> datetime:
    return datetime(2026, 9, 2, hour, 0, tzinfo=UTC)


def test_initial_state_is_subjective_baseline() -> None:
    state = CharacterState.initial(at=_at())

    assert state.state_version == 1
    assert state.relationship_tone == "baseline"
    rendered = state.render_prompt_context(at=_at())
    assert "SUBJECTIVE, MUTABLE, MAY BE WRONG" in rendered
    assert "relationship_tone: baseline" in rendered


def test_state_fields_are_normalized_and_time_aware() -> None:
    residue = StateResidue(text="  mildly annoyed  ", expires_at=_at(13))
    state = CharacterState(
        state_version=1,
        updated_at=_at(),
        emotional_stance=residue,
        relationship_tone="  familiar teasing  ",
        avoid_sounding_like=("  customer support  ", "overly agreeable"),
        open_thread_ids=(" thread_1 ",),
    )

    assert residue.text == "mildly annoyed"
    assert state.relationship_tone == "familiar teasing"
    assert state.avoid_sounding_like == ("customer support", "overly agreeable")
    assert state.open_thread_ids == ("thread_1",)

    with pytest.raises(ValueError, match="timezone-aware"):
        CharacterState.initial(at=datetime(2026, 9, 2, 12, 0))


def test_effective_view_drops_expired_and_rejected_subjective_state() -> None:
    state = CharacterState(
        state_version=1,
        updated_at=_at(),
        emotional_stance=StateResidue(text="annoyed", expires_at=_at(13)),
        current_preoccupations=(
            StateResidue(text="memory redesign", expires_at=_at(14)),
            StateResidue(text="old distraction", expires_at=_at(12)),
        ),
        working_assumptions=(
            WorkingAssumption(
                claim="user may prefer concise replies",
                confidence=0.6,
                source_message_ids=(10,),
                expires_at=_at(14),
            ),
            WorkingAssumption(
                claim="rejected guess",
                confidence=0.4,
                status=AssumptionStatus.REJECTED,
                source_message_ids=(11,),
                expires_at=_at(14),
            ),
        ),
    )

    effective = state.effective(at=_at(13))

    assert effective.emotional_stance is None
    assert tuple(item.text for item in effective.current_preoccupations) == ("memory redesign",)
    assert tuple(item.claim for item in effective.working_assumptions) == (
        "user may prefer concise replies",
    )
    assert state.emotional_stance is not None


def test_plausible_mistakes_have_bounded_confidence_and_provenance() -> None:
    with pytest.raises(ValueError, match="provenance"):
        PlausibleMistake(
            claim="unsupported guess",
            confidence=0.5,
            ttl_turns=3,
        )

    with pytest.raises(ValueError, match="less than or equal to 0.65"):
        PlausibleMistake(
            claim="too certain",
            confidence=0.8,
            ttl_turns=3,
            source_message_ids=(1,),
        )


def test_advance_turn_ages_plausible_mistakes_without_llm() -> None:
    state = CharacterState(
        state_version=1,
        updated_at=_at(),
        plausible_mistakes=(
            PlausibleMistake(
                claim="user may be awake because of project work",
                confidence=0.5,
                scope=MistakeScope.CASUAL_ONLY,
                ttl_turns=2,
                source_message_ids=(20,),
                expires_at=_at(16),
            ),
        ),
    )

    once = state.advance_turn(at=_at(13))
    twice = once.advance_turn(at=_at(14))

    assert once.state_version == 2
    assert once.plausible_mistakes[0].ttl_turns == 1
    assert twice.state_version == 3
    assert twice.plausible_mistakes == ()


def test_snapshot_round_trip_preserves_typed_state() -> None:
    state = CharacterState(
        state_version=3,
        updated_at=_at(),
        relationship_tone="familiar_teasing",
        emotional_stance=StateResidue(
            text="cooling down",
            expires_at=_at() + timedelta(hours=2),
        ),
        working_assumptions=(
            WorkingAssumption(
                claim="user may value continuity",
                confidence=0.7,
                source_memory_ids=("mem_1",),
                expires_at=_at() + timedelta(days=2),
            ),
        ),
        open_thread_ids=("thread_1",),
    )

    restored = CharacterState.from_snapshot(state.to_snapshot())

    assert restored == state
