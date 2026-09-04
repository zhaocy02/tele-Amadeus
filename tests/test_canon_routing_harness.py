import json
import subprocess
import sys
from pathlib import Path

from amadeus_bot.character.persona_evaluation import load_persona_evaluation_suite

DEFAULT_EXPECTATIONS = Path("profiles/v2/canon_routing_eval_cases.json")
DEFAULT_PERSONA_CASES = Path("profiles/v2/persona_eval_cases.json")
SCRIPT = Path("scripts/eval_canon_routing.py")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def test_default_routing_expectations_reference_existing_persona_cases() -> None:
    persona_suite = load_persona_evaluation_suite(DEFAULT_PERSONA_CASES)
    known_ids = {case.case_id for case in persona_suite.cases}
    raw = json.loads(DEFAULT_EXPECTATIONS.read_text(encoding="utf-8"))

    assert raw["suite_version"] == "canon-source-routing-v2-focused-v1"
    assert raw["expected_canon_count"] == 922
    expectation_ids = {item["case_id"] for item in raw["cases"]}
    assert expectation_ids <= known_ids
    assert "amadeus-memory-identity" in expectation_ids
    assert "amadeus-restart-continuity" in expectation_ids
    assert "established-contact-instrumentalization" in expectation_ids
    assert "do-not-just-agree" in expectation_ids
    shared_history = next(
        item for item in raw["cases"] if item["case_id"] == "canon-shared-history-boundary"
    )
    assert shared_history["max_results"] == 0


def test_routing_harness_runs_without_provider_and_reports_source_policy(tmp_path: Path) -> None:
    persona_cases = tmp_path / "cases.json"
    expectations = tmp_path / "expectations.json"
    canon = tmp_path / "canon.jsonl"

    _write_json(
        persona_cases,
        {
            "schema_version": 1,
            "suite_version": "routing-test-persona-v1",
            "cases": [
                {
                    "case_id": "ordinary-science",
                    "category": "science",
                    "user_message": "实验数据不足，能直接下结论吗？",
                    "expected_traits": ["检查证据"],
                    "avoid_traits": [],
                },
                {
                    "case_id": "restart",
                    "category": "amadeus_identity",
                    "user_message": "你只是个程序，关掉你再重启就一样吧？",
                    "expected_traits": ["处理连续性"],
                    "avoid_traits": [],
                },
            ],
        },
    )
    _write_json(
        expectations,
        {
            "schema_version": 1,
            "suite_version": "routing-harness-test-v1",
            "persona_suite": str(persona_cases),
            "expected_canon_count": 3,
            "cases": [
                {
                    "case_id": "ordinary-science",
                    "expected_delta": False,
                    "allowed_personas": ["kurisu"],
                    "min_results": 1,
                    "required_persona": None,
                    "required_delta_concepts": [],
                },
                {
                    "case_id": "restart",
                    "expected_delta": True,
                    "allowed_personas": ["kurisu", "amadeus"],
                    "min_results": 1,
                    "required_persona": "amadeus",
                    "required_delta_concepts": [
                        "digital_identity",
                        "restart_deletion_continuity",
                    ],
                },
            ],
        },
    )
    _write_jsonl(
        canon,
        [
            {
                "id": "sg:kurisu:science",
                "source": "sg",
                "persona": "kurisu",
                "history": [],
                "response": "先看数据。",
                "act": "DIRECT_ANSWER",
                "tags": ["实验", "数据", "证据"],
                "search_summary": "实验数据不足时先检查证据，不要直接下结论。",
            },
            {
                "id": "sg0:amadeus:science",
                "source": "sg0",
                "persona": "amadeus",
                "history": [],
                "response": "先看数据。",
                "act": "DIRECT_ANSWER",
                "tags": ["实验", "数据", "证据"],
                "search_summary": "实验数据不足时先检查证据，不要直接下结论。",
            },
            {
                "id": "sg0:amadeus:restart",
                "source": "sg0",
                "persona": "amadeus",
                "history": [],
                "response": "别把重启和连续性混为一谈。",
                "act": "DISAGREE",
                "tags": ["程序", "重启", "连续性", "数字身份"],
                "search_summary": "被当成可随意关机重启的程序时，会讨论数字自我的连续性。",
            },
        ],
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--canon",
            str(canon),
            "--expectations",
            str(expectations),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(completed.stdout)
    assert report["ok"] is True
    assert report["canon_example_count"] == 3
    ordinary, restart = report["cases"]
    assert ordinary["amadeus_delta"] is False
    assert [item["persona"] for item in ordinary["retrieved"]] == ["kurisu"]
    assert restart["amadeus_delta"] is True
    assert "restart_deletion_continuity" in restart["delta_concepts"]
    assert any(item["persona"] == "amadeus" for item in restart["retrieved"])


def test_routing_harness_enforces_max_results(tmp_path: Path) -> None:
    persona_cases = tmp_path / "cases.json"
    expectations = tmp_path / "expectations.json"
    canon = tmp_path / "canon.jsonl"

    _write_json(
        persona_cases,
        {
            "schema_version": 1,
            "suite_version": "routing-max-test-v1",
            "cases": [
                {
                    "case_id": "ordinary",
                    "category": "control",
                    "user_message": "实验数据不足",
                    "expected_traits": ["自然"],
                }
            ],
        },
    )
    _write_json(
        expectations,
        {
            "schema_version": 1,
            "suite_version": "routing-max-expectation-v1",
            "persona_suite": str(persona_cases),
            "expected_canon_count": 1,
            "cases": [
                {
                    "case_id": "ordinary",
                    "expected_delta": False,
                    "allowed_personas": ["kurisu"],
                    "min_results": 0,
                    "max_results": 0,
                    "required_persona": None,
                    "required_delta_concepts": [],
                }
            ],
        },
    )
    _write_jsonl(
        canon,
        [
            {
                "id": "sg:kurisu:science",
                "source": "sg",
                "persona": "kurisu",
                "history": [],
                "response": "先看数据。",
                "tags": ["实验", "数据"],
                "search_summary": "实验数据不足时先检查。",
            }
        ],
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--canon",
            str(canon),
            "--expectations",
            str(expectations),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report["ok"] is False
    assert "expected at most 0 results, got 1" in report["cases"][0]["reasons"]


def test_routing_harness_rejects_wrong_corpus_count(tmp_path: Path) -> None:
    persona_cases = tmp_path / "cases.json"
    expectations = tmp_path / "expectations.json"
    canon = tmp_path / "canon.jsonl"

    _write_json(
        persona_cases,
        {
            "schema_version": 1,
            "suite_version": "routing-count-test-v1",
            "cases": [
                {
                    "case_id": "ordinary",
                    "category": "control",
                    "user_message": "早上好",
                    "expected_traits": ["自然"],
                }
            ],
        },
    )
    _write_json(
        expectations,
        {
            "schema_version": 1,
            "suite_version": "routing-count-expectation-v1",
            "persona_suite": str(persona_cases),
            "expected_canon_count": 2,
            "cases": [
                {
                    "case_id": "ordinary",
                    "expected_delta": False,
                    "allowed_personas": ["kurisu"],
                    "min_results": 0,
                    "required_persona": None,
                    "required_delta_concepts": [],
                }
            ],
        },
    )
    _write_jsonl(
        canon,
        [
            {
                "id": "sg:kurisu:only",
                "source": "sg",
                "persona": "kurisu",
                "history": [],
                "response": "早。",
            }
        ],
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--canon",
            str(canon),
            "--expectations",
            str(expectations),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "routing corpus count mismatch" in completed.stderr
