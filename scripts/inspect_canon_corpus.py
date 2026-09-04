from __future__ import annotations

import argparse
import json
from pathlib import Path

from amadeus_bot.character.canon_inspection import inspect_canon_corpus


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and summarize one or more local Canon JSONL files before enrichment, "
            "distillation, or evaluation."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        nargs="+",
        required=True,
        help="one or more Canon JSONL files",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="optional JSON report path",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = inspect_canon_corpus(tuple(args.input))
    payload = report.to_dict()

    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
