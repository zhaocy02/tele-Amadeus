import json
from pathlib import Path

import pytest

from amadeus_bot.character.canon import CanonRetriever, load_canon_examples


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_load_canon_examples_preserves_provenance_fields(tmp_path: Path) -> None:
    corpus_path = tmp_path / "canon.jsonl"
    _write_jsonl(
        corpus_path,
        [
            {
                "id": "sg:kurisu:000001",
                "source": "sg",
                "persona": "kurisu",
                "history": ["Friend: 这个结论没有实验数据。"],
                "response": "那就先补数据，别急着下结论。",
                "act": "CHALLENGE",
                "tags": ["science", "evidence"],
                "search_summary": "对方缺少实验依据时，先要求证据再讨论结论。",
            }
        ],
    )

    examples = load_canon_examples(corpus_path)

    assert len(examples) == 1
    example = examples[0]
    assert example.example_id == "sg:kurisu:000001"
    assert example.source == "sg"
    assert example.persona == "kurisu"
    assert example.act == "CHALLENGE"
    assert example.tags == ("science", "evidence")


def test_retrieval_prefers_relevant_behavior_summary(tmp_path: Path) -> None:
    corpus_path = tmp_path / "canon.jsonl"
    _write_jsonl(
        corpus_path,
        [
            {
                "id": "science",
                "source": "sg",
                "persona": "kurisu",
                "history": ["Friend: 我没测，但我觉得一定是这样。"],
                "response": "没有数据就别把猜测说成结论。",
                "act": "CHALLENGE",
                "tags": ["science", "evidence"],
                "search_summary": "面对没有实验数据的断言，要求证据并纠正过度结论。",
            },
            {
                "id": "teasing",
                "source": "sg",
                "persona": "kurisu",
                "history": ["Friend: 你是不是害羞了？"],
                "response": "谁、谁害羞了？少自作多情。",
                "act": "TEASE",
                "tags": ["banter", "embarrassment"],
                "search_summary": "被亲近的人调侃害羞时先否认并反击，但继续交流。",
            },
        ],
    )
    retriever = CanonRetriever(load_canon_examples(corpus_path), min_score=0.05)

    result = retriever.retrieve("我这次实验没有数据，但应该可以直接得出结论吧？")

    assert result.example_ids == ("science",)
    assert result.items[0].score.lexical > 0.0


def test_retrieval_allows_zero_examples_when_no_analogy_is_relevant(tmp_path: Path) -> None:
    corpus_path = tmp_path / "canon.jsonl"
    _write_jsonl(
        corpus_path,
        [
            {
                "id": "science",
                "source": "sg",
                "persona": "kurisu",
                "history": [],
                "response": "先看实验数据。",
                "act": "CHALLENGE",
                "tags": ["science"],
                "search_summary": "讨论实验数据与科学证据。",
            }
        ],
    )
    retriever = CanonRetriever(load_canon_examples(corpus_path), min_score=0.05)

    result = retriever.retrieve("今晚想吃什么？", act="CHALLENGE")

    assert result.items == ()


def test_retrieval_uses_policy_act_only_as_bonus_not_as_relevance_gate(tmp_path: Path) -> None:
    corpus_path = tmp_path / "canon.jsonl"
    _write_jsonl(
        corpus_path,
        [
            {
                "id": "matching-act",
                "source": "sg",
                "persona": "kurisu",
                "history": [],
                "response": "没有证据。",
                "act": "CHALLENGE",
                "tags": ["science"],
                "search_summary": "科学讨论中要求证据。",
            },
            {
                "id": "other-act",
                "source": "sg",
                "persona": "kurisu",
                "history": [],
                "response": "那就先把数据给我。",
                "act": "DIRECT_ANSWER",
                "tags": ["science"],
                "search_summary": "科学讨论中要求证据。",
            },
        ],
    )
    retriever = CanonRetriever(load_canon_examples(corpus_path), min_score=0.05)

    result = retriever.retrieve("科学证据在哪里？", act="CHALLENGE", limit=2)

    assert result.example_ids == ("matching-act", "other-act")
    assert result.items[0].score.act == 1.0
    assert result.items[1].score.act == 0.0


def test_load_canon_examples_rejects_duplicate_ids(tmp_path: Path) -> None:
    corpus_path = tmp_path / "canon.jsonl"
    record = {
        "id": "duplicate",
        "source": "sg",
        "persona": "kurisu",
        "history": [],
        "response": "test",
    }
    _write_jsonl(corpus_path, [record, record])

    with pytest.raises(ValueError, match="duplicate canon example id"):
        load_canon_examples(corpus_path)
