from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from amadeus_bot.character.canon import load_canon_examples
from amadeus_bot.character.persona import load_persona_core
from amadeus_bot.character.persona_evaluation import (
    PersonaEvaluationRunner,
    load_persona_distillation_candidate,
    load_persona_evaluation_suite,
    write_persona_evaluation_report,
)
from amadeus_bot.character.persona_evaluation_checkpoint import (
    remove_persona_evaluation_checkpoint,
    run_persona_evaluation_with_checkpoint,
)
from amadeus_bot.llm import ResponsesAPIProvider


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run fixed Kurisu/Amadeus persona cases across baseline/candidate and optional canon "
            "RAG variants. Produces an inspectable report; it does not assign an opaque score. "
            "Completed observations are checkpointed so interrupted runs can resume safely."
        )
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=Path("profiles/v2/persona_eval_cases.json"),
        help="versioned persona evaluation case suite",
    )
    parser.add_argument(
        "--persona",
        type=Path,
        default=Path("profiles/v2/persona_core.json"),
        help="baseline Persona Core",
    )
    candidate_group = parser.add_mutually_exclusive_group()
    candidate_group.add_argument(
        "--candidate",
        type=Path,
        default=None,
        help="optional Canon distillation candidate JSON applied as an additive evaluation overlay",
    )
    candidate_group.add_argument(
        "--candidate-persona",
        type=Path,
        default=None,
        help="optional complete Persona Core candidate JSON for exact-file A/B evaluation",
    )
    parser.add_argument(
        "--canon",
        type=Path,
        default=None,
        help="optional enriched canon JSONL for RAG-on variants",
    )
    parser.add_argument("--output", type=Path, required=True, help="evaluation report JSON")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="optional checkpoint path; defaults to <output>.checkpoint.json",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="optional model override; defaults to AMADEUS_PROVIDER_MODEL",
    )
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def _timeout_from_env() -> float:
    raw = os.environ.get("AMADEUS_REQUEST_TIMEOUT_SECONDS", "120").strip() or "120"
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise SystemExit("AMADEUS_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
    if timeout <= 0:
        raise SystemExit("AMADEUS_REQUEST_TIMEOUT_SECONDS must be positive")
    return timeout


def _default_checkpoint(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".checkpoint.json")


def _print_progress(completed: int, total: int, variant: str, case_id: str) -> None:
    print(f"[eval] {completed}/{total} | {variant} | {case_id}", flush=True)


async def _run(args: argparse.Namespace) -> int:
    suite = load_persona_evaluation_suite(args.cases)
    persona = load_persona_core(args.persona)
    candidate = (
        load_persona_distillation_candidate(args.candidate) if args.candidate is not None else None
    )
    candidate_persona = (
        load_persona_core(args.candidate_persona) if args.candidate_persona is not None else None
    )
    canon_examples = load_canon_examples(args.canon) if args.canon is not None else ()

    if candidate is not None and candidate.current_persona_version != persona.core.persona_version:
        raise SystemExit(
            "candidate Persona Core version mismatch: "
            f"candidate={candidate.current_persona_version} current={persona.core.persona_version}"
        )
    if (
        candidate_persona is not None
        and candidate_persona.core.persona_version == persona.core.persona_version
    ):
        raise SystemExit("candidate Persona Core must use a distinct persona_version")

    base_url = _required_env("AMADEUS_PROVIDER_BASE_URL")
    api_key = os.environ.get("AMADEUS_PROVIDER_API_KEY", "").strip() or None
    model = args.model or os.environ.get("AMADEUS_PROVIDER_MODEL", "gpt-5.6-sol").strip()
    if not model:
        raise SystemExit("provider model must not be empty")

    checkpoint_path = args.checkpoint or _default_checkpoint(args.output)
    provider = ResponsesAPIProvider(
        base_url=base_url,
        api_key=api_key,
        default_model=model,
        reasoning_effort="medium",
        timeout_seconds=_timeout_from_env(),
    )
    try:
        runner = PersonaEvaluationRunner(
            provider=provider,
            base_persona=persona,
            suite=suite,
            model=model,
            candidate=candidate,
            candidate_persona=candidate_persona,
            canon_examples=canon_examples,
        )
        report, stats = await run_persona_evaluation_with_checkpoint(
            runner,
            checkpoint_path=checkpoint_path,
            progress=_print_progress,
        )
        write_persona_evaluation_report(args.output, report)
        remove_persona_evaluation_checkpoint(checkpoint_path)
    finally:
        await provider.aclose()

    variants = tuple(dict.fromkeys(item.variant for item in report.observations))
    print(
        f"evaluated {len(report.cases)} cases across {len(variants)} variants "
        f"({', '.join(variants)}) -> {args.output}"
    )
    print(
        f"observations this run: {stats.provider_observations}; "
        f"resumed from checkpoint: {stats.resumed_observations}; "
        f"total observations: {stats.total_observations}"
    )
    print("report contains raw observations for review; no automatic persona score was assigned")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
