from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from pathlib import Path

from amadeus_bot.character.canon import CanonExample, load_canon_examples
from amadeus_bot.character.canon_enrichment import (
    CanonEnrichmentRunStats,
    enrich_canon_with_checkpoint,
    remove_enrichment_checkpoint,
    write_enriched_canon,
)
from amadeus_bot.character.canon_inspection import inspect_canon_corpus
from amadeus_bot.llm import ResponsesAPIProvider


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline-enrich a raw canon JSONL corpus with concise Chinese behavior summaries, "
            "policy acts, and search tags. Uses AMADEUS_PROVIDER_* environment variables but "
            "does not require Telegram configuration. Completed provider batches are checkpointed "
            "so interrupted runs can resume safely."
        )
    )
    parser.add_argument("--input", type=Path, required=True, help="raw canon JSONL")
    parser.add_argument("--output", type=Path, required=True, help="enriched canon JSONL")
    parser.add_argument("--batch-size", type=int, default=8, help="records per provider request")
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
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="optional run report path; defaults to <output>.report.json",
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


def _default_sidecar(path: Path, suffix: str) -> Path:
    return path.with_suffix(path.suffix + suffix)


def _source_persona_counts(examples: tuple[CanonExample, ...]) -> Counter[str]:
    return Counter(f"{example.source}/{example.persona}" for example in examples)


def _build_report(
    raw: tuple[CanonExample, ...],
    enriched: tuple[CanonExample, ...],
    stats: CanonEnrichmentRunStats,
    *,
    model: str,
) -> dict[str, object]:
    raw_counts = _source_persona_counts(raw)
    kept_counts = _source_persona_counts(enriched)
    by_source_persona: dict[str, object] = {}
    for key in sorted(raw_counts):
        raw_count = raw_counts[key]
        kept_count = kept_counts.get(key, 0)
        by_source_persona[key] = {
            "raw": raw_count,
            "kept": kept_count,
            "dropped": raw_count - kept_count,
            "keep_rate": kept_count / raw_count if raw_count else 0.0,
        }

    return {
        "model": model,
        "raw_examples": len(raw),
        "kept_examples": len(enriched),
        "dropped_examples": len(raw) - len(enriched),
        "keep_rate": len(enriched) / len(raw) if raw else 0.0,
        "pre_enriched_examples": stats.pre_enriched_examples,
        "resumed_examples": stats.resumed_examples,
        "provider_examples_this_run": stats.provider_examples,
        "by_source_persona": by_source_persona,
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def _run(args: argparse.Namespace) -> int:
    preflight = inspect_canon_corpus((args.input,))
    if not preflight.ok:
        raise SystemExit(
            "canon input failed preflight; fix duplicate/provenance problems before provider calls"
        )

    examples = load_canon_examples(args.input)
    base_url = _required_env("AMADEUS_PROVIDER_BASE_URL")
    api_key = os.environ.get("AMADEUS_PROVIDER_API_KEY", "").strip() or None
    model = args.model or os.environ.get("AMADEUS_PROVIDER_MODEL", "gpt-5.6-sol").strip()
    if not model:
        raise SystemExit("provider model must not be empty")

    checkpoint_path = args.checkpoint or _default_sidecar(args.output, ".checkpoint.json")
    report_path = args.report or _default_sidecar(args.output, ".report.json")

    provider = ResponsesAPIProvider(
        base_url=base_url,
        api_key=api_key,
        default_model=model,
        reasoning_effort="low",
        timeout_seconds=_timeout_from_env(),
    )
    try:
        enriched, stats = await enrich_canon_with_checkpoint(
            examples,
            provider=provider,
            checkpoint_path=checkpoint_path,
            model=model,
            batch_size=args.batch_size,
        )
        write_enriched_canon(args.output, enriched)
        report = _build_report(examples, enriched, stats, model=model)
        _write_report(report_path, report)
        remove_enrichment_checkpoint(checkpoint_path)
    finally:
        await provider.aclose()

    print(
        f"enriched {len(examples)} source examples -> {len(enriched)} kept behavior examples "
        f"at {args.output}"
    )
    print(
        f"provider processed this run: {stats.provider_examples}; "
        f"resumed from checkpoint: {stats.resumed_examples}"
    )
    print(f"report: {report_path}")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
