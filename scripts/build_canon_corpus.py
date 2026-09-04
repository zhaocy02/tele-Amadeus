from __future__ import annotations

import argparse
from pathlib import Path

from amadeus_bot.character.canon_ingest import (
    build_records,
    load_dialogue_source,
    write_jsonl,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a local canon JSONL corpus from either Speaker: dialogue text or sc3tools "
            "`.scx.txt` output. The speaker format supports community sources such as "
            "FrancescoCaracciolo/Amadeus; sc3 format supports legally extracted SG/SG0 scripts."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="input text file, or a directory of sc3tools .txt files when --format=sc3",
    )
    parser.add_argument("--output", type=Path, required=True, help="output canon JSONL path")
    parser.add_argument("--source", required=True, help="source id, for example sg or sg0")
    parser.add_argument(
        "--persona",
        required=True,
        help="persona id, for example kurisu or amadeus",
    )
    parser.add_argument(
        "--speaker",
        action="append",
        default=None,
        help="speaker name to extract; repeat for aliases (default: Kurisu)",
    )
    parser.add_argument(
        "--format",
        choices=("speaker", "sc3"),
        default="speaker",
        help="input format: Speaker: dialogue or raw sc3tools text",
    )
    parser.add_argument(
        "--history-size",
        type=int,
        default=3,
        help="number of preceding utterances kept as scene context",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.history_size < 0:
        raise SystemExit("--history-size must not be negative")

    try:
        utterances = load_dialogue_source(args.input, input_format=args.format)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    speakers = tuple(args.speaker or ("Kurisu",))
    records = build_records(
        utterances,
        source=args.source.strip(),
        persona=args.persona.strip(),
        speaker=speakers,
        history_size=args.history_size,
    )
    write_jsonl(records, args.output)
    print(f"wrote {len(records)} canon examples to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
