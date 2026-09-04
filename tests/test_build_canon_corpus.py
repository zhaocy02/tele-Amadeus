from pathlib import Path

from amadeus_bot.character.canon_ingest import (
    Utterance,
    build_records,
    load_dialogue_source,
    parse_sc3_dialogue,
    parse_speaker_dialogue,
)


def test_parse_speaker_dialogue_preserves_continuation_lines() -> None:
    text = """
Friend: I have a theory.
It definitely works.

Kurisu: You tested it, right?
Because otherwise it's only a guess.

Friend: Not yet.
"""

    utterances = parse_speaker_dialogue(text)

    assert len(utterances) == 3
    assert utterances[0].speaker == "Friend"
    assert utterances[0].text == "I have a theory. It definitely works."
    assert utterances[1].speaker == "Kurisu"
    assert utterances[1].text == "You tested it, right? Because otherwise it's only a guess."


def test_parse_sc3_dialogue_matches_mages_name_line_format() -> None:
    text = """
[name]Maho[line]“Are you listening?”[%p]
[name]Amadeus[line]“Obviously.”[%p]
[name]Amadeus[line]“I was waiting for you to finish.”[%p]
The camera turned toward me.[%p]
[name]Rintaro[line]“Right...”[%p]
"""

    utterances = parse_sc3_dialogue(text)

    assert utterances == (
        _utterance("Maho", "Are you listening?"),
        _utterance("Amadeus", "Obviously. I was waiting for you to finish."),
        _utterance("UNSPOKEN", "The camera turned toward me."),
        _utterance("Rintaro", "Right..."),
    )


def test_parse_sc3_dialogue_preserves_multiline_mages_record() -> None:
    text = """
[name]Kurisu[line]“I know you used the Time Leap Machine.
”[%p]
[name]Kurisu[line]“Not just for your sake,
but for mine as well.”[%18]
[name]Rintaro[line]“Right...”[%p]
"""

    utterances = parse_sc3_dialogue(text)

    assert utterances == (
        _utterance(
            "Kurisu",
            "I know you used the Time Leap Machine. Not just for your sake, but for mine as well.",
        ),
        _utterance("Rintaro", "Right..."),
    )


def test_parse_sc3_dialogue_strips_engine_tags_and_accepts_non_p_terminator() -> None:
    text = """
[margin top="38"][name]Kurisu[line]“Check the [color index="1"]data first.”[%p]
[name]Kurisu[line]“Please, Okabe-san...”[%18]
"""

    utterances = parse_sc3_dialogue(text)

    assert utterances == (
        _utterance("Kurisu", "Check the data first. Please, Okabe-san..."),
    )


def test_sc3_directory_keeps_file_order_and_blocks_cross_file_history(tmp_path: Path) -> None:
    (tmp_path / "SG0_02.scx.txt").write_text(
        '[name]Maho[line]“Second scene setup.”[%p]\n'
        '[name]Amadeus[line]“Second scene response.”[%p]\n',
        encoding="utf-8",
    )
    (tmp_path / "SG0_01.scx.txt").write_text(
        '[name]Maho[line]“First scene setup.”[%p]\n'
        '[name]Amadeus[line]“First scene response.”[%p]\n',
        encoding="utf-8",
    )

    utterances = load_dialogue_source(tmp_path, input_format="sc3")
    records = build_records(
        utterances,
        source="sg0",
        persona="amadeus",
        speaker="Amadeus",
        history_size=3,
    )

    assert len(records) == 2
    assert records[0]["response"] == "First scene response."
    assert records[0]["history"] == ["Maho: First scene setup."]
    assert records[1]["response"] == "Second scene response."
    assert records[1]["history"] == ["Maho: Second scene setup."]


def test_directory_input_rejects_speaker_format(tmp_path: Path) -> None:
    (tmp_path / "dialogue.txt").write_text("Kurisu: hello\n", encoding="utf-8")

    try:
        load_dialogue_source(tmp_path, input_format="speaker")
    except ValueError as exc:
        assert "only with input_format='sc3'" in str(exc)
    else:
        raise AssertionError("speaker-format directory input should fail")


def test_build_records_keeps_small_scene_window() -> None:
    utterances = parse_speaker_dialogue(
        """
Friend: First message.
Other: Second message.
Friend: Third message.
Kurisu: Response one.
Friend: Fourth message.
Kurisu: Response two.
"""
    )

    records = build_records(
        utterances,
        source="sg",
        persona="kurisu",
        speaker="Kurisu",
        history_size=2,
    )

    assert len(records) == 2
    assert records[0]["id"] == "sg:kurisu:000000"
    assert records[0]["history"] == ["Other: Second message.", "Friend: Third message."]
    assert records[0]["response"] == "Response one."
    assert records[1]["history"] == ["Kurisu: Response one.", "Friend: Fourth message."]


def test_build_records_can_target_amadeus_speaker_name() -> None:
    utterances = parse_speaker_dialogue(
        """
Maho: Are you there?
Amadeus: Obviously.
Maho: That's reassuring.
"""
    )

    records = build_records(
        utterances,
        source="sg0",
        persona="amadeus",
        speaker="Amadeus",
        history_size=3,
    )

    assert len(records) == 1
    assert records[0]["source"] == "sg0"
    assert records[0]["persona"] == "amadeus"
    assert records[0]["history"] == ["Maho: Are you there?"]
    assert records[0]["response"] == "Obviously."


def _utterance(speaker: str, text: str) -> Utterance:
    return Utterance(speaker=speaker, text=text)
