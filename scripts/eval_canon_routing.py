from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from amadeus_bot.character.canon import CanonRetriever, load_canon_examples
from amadeus_bot.character.persona_evaluation import load_persona_evaluation_suite

DEFAULT_EXPECTATIONS = Path("profiles/v2/canon_routing_eval_cases.json")


class RoutingExpectation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    expected_delta: bool
    allowed_personas: tuple[Literal["kurisu", "amadeus"], ...]
    min_results: int = Field(ge=0, le=4)
    max_results: int | None = Field(default=None, ge=0, le=4)
    required_persona: Literal["kurisu", "amadeus"] | None = None
    required_delta_concepts: tuple[str, ...] = ()

    @field_validator("case_id")
    @classmethod
    def validate_case_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("routing expectation case_id must not be empty")
        return cleaned

    @field_validator("allowed_personas")
    @classmethod
    def validate_allowed_personas(
        cls,
        value: tuple[Literal["kurisu", "amadeus"], ...],
    ) -> tuple[Literal["kurisu", "amadeus"], ...]:
        if not value:
            raise ValueError("allowed_personas must not be empty")
        return value

    @field_validator("required_delta_concepts")
    @classmethod
    def validate_required_delta_concepts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item.strip() for item in value if item.strip())


class RoutingExpectationSuite(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    suite_version: str
    persona_suite: str
    expected_canon_count: int | None = Field(default=None, ge=1)
    cases: tuple[RoutingExpectation, ...]

    @field_validator("suite_version", "persona_suite")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("routing suite text fields must not be empty")
        return cleaned

    @field_validator("cases")
    @classmethod
    def validate_cases(
        cls,
        value: tuple[RoutingExpectation, ...],
    ) -> tuple[RoutingExpectation, ...]:
        if not value:
            raise ValueError("routing suite must contain at least one case")
        case_ids = tuple(item.case_id for item in value)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("routing expectation case ids must be unique")
        for item in value:
            if item.max_results is not None and item.max_results < item.min_results:
                raise ValueError(
                    f"routing expectation max_results must be >= min_results: {item.case_id}"
                )
        return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic Canon source-routing regression against a local enriched corpus. "
            "No provider/LLM call is made."
        )
    )
    parser.add_argument("--canon", type=Path, required=True, help="enriched Canon JSONL path")
    parser.add_argument(
        "--expectations",
        type=Path,
        default=DEFAULT_EXPECTATIONS,
        help=f"routing expectation JSON (default: {DEFAULT_EXPECTATIONS})",
    )
    parser.add_argument(
        "--persona-cases",
        type=Path,
        default=None,
        help="optional override for the Persona evaluation suite referenced by expectations",
    )
    parser.add_argument("--min-score", type=float, default=0.10)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--output", type=Path, default=None, help="optional JSON report path")
    return parser


def _load_expectations(path: Path) -> RoutingExpectationSuite:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return RoutingExpectationSuite.model_validate(raw)
    except OSError as exc:
        raise ValueError(f"unable to read routing expectations: {path}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid routing expectations: {path}") from exc


def _evaluate(args: argparse.Namespace) -> dict[str, object]:
    expectations = _load_expectations(args.expectations)
    persona_cases_path = args.persona_cases or Path(expectations.persona_suite)
    persona_suite = load_persona_evaluation_suite(persona_cases_path)
    case_map = {case.case_id: case for case in persona_suite.cases}

    missing = [item.case_id for item in expectations.cases if item.case_id not in case_map]
    if missing:
        raise ValueError(f"routing expectation references unknown Persona cases: {missing}")

    canon_examples = load_canon_examples(args.canon)
    if (
        expectations.expected_canon_count is not None
        and len(canon_examples) != expectations.expected_canon_count
    ):
        raise ValueError(
            "routing corpus count mismatch: "
            f"expected {expectations.expected_canon_count}, got {len(canon_examples)}"
        )

    retriever = CanonRetriever(canon_examples, min_score=args.min_score)
    case_reports: list[dict[str, object]] = []
    all_ok = True

    for expectation in expectations.cases:
        case = case_map[expectation.case_id]
        result = retriever.retrieve(case.user_message, limit=args.limit)
        personas = tuple(item.example.persona.casefold() for item in result.items)
        reasons: list[str] = []

        if result.amadeus_delta != expectation.expected_delta:
            reasons.append(
                f"expected delta={expectation.expected_delta}, got {result.amadeus_delta}"
            )
        disallowed = tuple(
            persona for persona in personas if persona not in expectation.allowed_personas
        )
        if disallowed:
            reasons.append(f"disallowed retrieved personas: {disallowed}")
        if len(result.items) < expectation.min_results:
            reasons.append(
                f"expected at least {expectation.min_results} results, got {len(result.items)}"
            )
        if expectation.max_results is not None and len(result.items) > expectation.max_results:
            reasons.append(
                f"expected at most {expectation.max_results} results, got {len(result.items)}"
            )
        if (
            expectation.required_persona is not None
            and expectation.required_persona not in personas
        ):
            reasons.append(f"required persona missing: {expectation.required_persona}")
        missing_concepts = tuple(
            concept
            for concept in expectation.required_delta_concepts
            if concept not in result.delta_concepts
        )
        if missing_concepts:
            reasons.append(f"required delta concepts missing: {missing_concepts}")

        case_ok = not reasons
        all_ok = all_ok and case_ok
        case_reports.append(
            {
                "case_id": case.case_id,
                "category": case.category,
                "query": case.user_message,
                "ok": case_ok,
                "reasons": reasons,
                "amadeus_delta": result.amadeus_delta,
                "delta_concepts": list(result.delta_concepts),
                "retrieved": [
                    {
                        "id": item.example.example_id,
                        "source": item.example.source,
                        "persona": item.example.persona,
                        "act": item.example.act,
                        "score": round(item.score.total, 6),
                        "lexical": round(item.score.lexical, 6),
                        "cue": round(item.score.cue, 6),
                    }
                    for item in result.items
                ],
            }
        )

    return {
        "schema_version": 1,
        "suite_version": expectations.suite_version,
        "persona_suite_version": persona_suite.suite_version,
        "canon_example_count": len(canon_examples),
        "min_score": args.min_score,
        "limit": args.limit,
        "ok": all_ok,
        "cases": case_reports,
    }


def main() -> int:
    args = _build_parser().parse_args()
    if not 0.0 <= args.min_score <= 1.0:
        raise SystemExit("--min-score must be between 0 and 1")
    if not 1 <= args.limit <= 4:
        raise SystemExit("--limit must be between 1 and 4")

    try:
        report = _evaluate(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(payload, end="")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
