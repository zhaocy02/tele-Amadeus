import json
from pathlib import Path

from amadeus_bot.character.canon_inspection import inspect_canon_corpus


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_inspection_reports_source_persona_and_enrichment_coverage(tmp_path: Path) -> None:
    sg = tmp_path / "sg.jsonl"
    sg0 = tmp_path / "sg0.jsonl"
    _write_jsonl(
        sg,
        [
            {
                "id": "sg:kurisu:000000",
                "source": "sg",
                "persona": "kurisu",
                "history": ["Okabe: Explain."],
                "response": "Show me the evidence.",
                "act": "challenge",
                "tags": ["证据"],
                "search_summary": "面对未经验证的结论时要求证据。",
            }
        ],
    )
    _write_jsonl(
        sg0,
        [
            {
                "id": "sg0:amadeus:000000",
                "source": "sg0",
                "persona": "amadeus",
                "history": [],
                "response": "Obviously.",
            },
            {
                "id": "sg0:kurisu:000000",
                "source": "sg0",
                "persona": "kurisu",
                "history": ["Maho: Are you sure?", "Kurisu: I checked."],
                "response": "Then check it again.",
            },
        ],
    )

    report = inspect_canon_corpus((sg, sg0))

    assert report.ok is True
    assert report.total_examples == 3
    assert report.counts_by_source == {"sg": 1, "sg0": 2}
    assert report.counts_by_persona == {"amadeus": 1, "kurisu": 2}
    assert report.counts_by_source_persona == {
        "sg/kurisu": 1,
        "sg0/amadeus": 1,
        "sg0/kurisu": 1,
    }
    assert report.examples_with_history == 2
    assert report.max_history_entries == 2
    assert report.examples_with_search_summary == 1
    assert report.examples_with_act == 1
    assert report.examples_with_tags == 1


def test_inspection_detects_duplicate_ids_across_input_files(tmp_path: Path) -> None:
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    record = {
        "id": "sg:kurisu:000000",
        "source": "sg",
        "persona": "kurisu",
        "history": [],
        "response": "One response.",
    }
    _write_jsonl(first, [record])
    _write_jsonl(second, [{**record, "response": "Another response."}])

    report = inspect_canon_corpus((first, second))

    assert report.ok is False
    assert report.duplicate_ids == ("sg:kurisu:000000",)


def test_inspection_detects_id_provenance_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "bad-prefix.jsonl"
    _write_jsonl(
        path,
        [
            {
                "id": "sg:amadeus:000000",
                "source": "sg0",
                "persona": "amadeus",
                "history": [],
                "response": "A response.",
            }
        ],
    )

    report = inspect_canon_corpus((path,))

    assert report.ok is False
    assert report.id_prefix_mismatches == ("sg:amadeus:000000",)
