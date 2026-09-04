from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from amadeus_bot.character.canon import load_canon_examples
from amadeus_bot.character.canon_distillation import (
    CanonPersonaDistiller,
    PersonaDistillationConfig,
    write_persona_distillation_candidate,
)
from amadeus_bot.character.canon_distillation_checkpoint import (
    distill_persona_with_checkpoint,
    remove_persona_distillation_checkpoint,
)
from amadeus_bot.character.persona import load_persona_core
from amadeus_bot.llm import ResponsesAPIProvider


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline-distill an enriched SG/SG0 canon corpus into an evidence-backed Persona Core "
            "candidate. The output is review-only and never overwrites Persona Core. Completed "
            "extraction batches are checkpointed so interrupted runs can resume safely."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="enriched canon JSONL")
    parser.add_argument(
        "--persona",
        type=Path,
        default=Path("profiles/v2/persona_core.json"),
        help="current Persona Core used as a comparison baseline",
    )
    parser.add_argument("--output", type=Path, required=True, help="candidate JSON output")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="canon examples per extract call",
    )
    parser.add_argument(
        "--consolidation-batch-size",
        type=int,
        default=20,
        help="provisional rules per consolidation call",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="optional model override; defaults to AMADEUS_PROVIDER_MODEL",
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=None,
        help="optional checkpoint path; defaults to <output>.checkpoint.json",
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


async def _run(args: argparse.Namespace) -> int:
    examples = load_canon_examples(args.input)
    persona = load_persona_core(args.persona)
    base_url = _required_env("AMADEUS_PROVIDER_BASE_URL")
    api_key = os.environ.get("AMADEUS_PROVIDER_API_KEY", "").strip() or None
    model = args.model or os.environ.get("AMADEUS_PROVIDER_MODEL", "gpt-5.6-sol").strip()
    if not model:
        raise SystemExit("provider model must not be empty")

    config = PersonaDistillationConfig(
        batch_size=args.batch_size,
        consolidation_batch_size=args.consolidation_batch_size,
    )
    checkpoint_path = args.checkpoint or _default_checkpoint(args.output)
    provider = ResponsesAPIProvider(
        base_url=base_url,
        api_key=api_key,
        default_model=model,
        reasoning_effort="medium",
        timeout_seconds=_timeout_from_env(),
    )
    try:
        distiller = CanonPersonaDistiller(
            provider=provider,
            model=model,
            config=config,
        )
        candidate, stats = await distill_persona_with_checkpoint(
            examples,
            distiller=distiller,
            current_persona=persona.core,
            config=config,
            checkpoint_path=checkpoint_path,
        )
        write_persona_distillation_candidate(args.output, candidate)
        remove_persona_distillation_checkpoint(checkpoint_path)
    finally:
        await provider.aclose()

    print(
        f"distilled {candidate.source_example_count} enriched canon examples -> "
        f"{len(candidate.rules)} candidate persona rules at {args.output}"
    )
    print(
        f"extraction batches this run: {stats.provider_extraction_batches}; "
        f"resumed extraction batches: {stats.resumed_extraction_batches}; "
        f"total extraction batches: {stats.total_extraction_batches}"
    )
    print(f"consolidation passes this run: {stats.consolidation_passes_this_run}")
    print("review-only output: Persona Core was not modified")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
