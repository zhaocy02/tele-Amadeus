from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

_RECORD_RE = re.compile(r".*?\[%[^\]]+\]", re.DOTALL)
_SPEAKER_RE = re.compile(r"\[name\](.*?)\[line\]", re.IGNORECASE | re.DOTALL)
_AMADEUS_LITERAL_RE = re.compile(r"\bamadeus\b", re.IGNORECASE)
_DIGITAL_CUE_RE = re.compile(
    r"\b(?:amadeus|phone|call|screen|application|app|server|login|digital|"
    r"artificial intelligence|AI)\b",
    re.IGNORECASE,
)
_FLASHBACK_CUE_RE = re.compile(
    r"\b(?:dream|memory|memories|remember|remembered|dead|died|death|past|"
    r"world\s*line|worldline|flashback)\b",
    re.IGNORECASE,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect SG0 sc3tools text for Kurisu/Amadeus speaker-label ambiguity without "
            "printing or storing VN dialogue."
        )
    )
    parser.add_argument("--txt-dir", type=Path, required=True, help="sc3tools txt directory")
    parser.add_argument("--output", type=Path, required=True, help="JSON diagnostic output")
    parser.add_argument(
        "--context-records",
        type=int,
        default=4,
        help="records before/after each Kurisu record used for cue counts",
    )
    return parser


def _records(text: str) -> tuple[str, ...]:
    records = tuple(match.group(0) for match in _RECORD_RE.finditer(text))
    if not records and text.strip():
        return (text,)
    # Only terminated dialogue records matter; ignore trailing engine text.
    return records


def _speaker(record: str) -> str:
    match = _SPEAKER_RE.search(record)
    if match is None:
        return ""
    return " ".join(match.group(1).split()).strip()


def inspect(txt_dir: Path, *, context_records: int) -> dict[str, object]:
    if context_records < 0:
        raise ValueError("context_records must not be negative")
    if not txt_dir.is_dir():
        raise ValueError(f"txt directory does not exist: {txt_dir}")

    files = sorted(path for path in txt_dir.rglob("*.txt") if path.is_file())
    if not files:
        raise ValueError(f"no .txt files found under: {txt_dir}")

    speaker_totals: Counter[str] = Counter()
    rows: list[dict[str, object]] = []
    total_kurisu = 0
    total_amadeus = 0
    total_kurisu_near_amadeus = 0
    total_kurisu_near_digital = 0
    total_kurisu_near_flashback = 0

    for path in files:
        text = path.read_text(encoding="utf-8")
        records = _records(text)
        speakers = tuple(_speaker(record) for record in records)
        file_counts = Counter(speaker for speaker in speakers if speaker)
        speaker_totals.update(file_counts)

        kurisu_count = file_counts.get("Kurisu", 0)
        amadeus_count = file_counts.get("Amadeus", 0)
        if kurisu_count == 0 and amadeus_count == 0 and not _AMADEUS_LITERAL_RE.search(text):
            continue

        near_amadeus = 0
        near_digital = 0
        near_flashback = 0
        for index, speaker in enumerate(speakers):
            if speaker.casefold() != "kurisu":
                continue
            start = max(0, index - context_records)
            end = min(len(records), index + context_records + 1)
            context = "\n".join(records[start:end])
            near_amadeus += int(bool(_AMADEUS_LITERAL_RE.search(context)))
            near_digital += int(bool(_DIGITAL_CUE_RE.search(context)))
            near_flashback += int(bool(_FLASHBACK_CUE_RE.search(context)))

        total_kurisu += kurisu_count
        total_amadeus += amadeus_count
        total_kurisu_near_amadeus += near_amadeus
        total_kurisu_near_digital += near_digital
        total_kurisu_near_flashback += near_flashback
        rows.append(
            {
                "path": str(path.relative_to(txt_dir)),
                "kurisu": kurisu_count,
                "amadeus": amadeus_count,
                "amadeus_literal_occurrences": len(_AMADEUS_LITERAL_RE.findall(text)),
                "kurisu_near_amadeus_literal": near_amadeus,
                "kurisu_near_digital_cues": near_digital,
                "kurisu_near_flashback_cues": near_flashback,
            }
        )

    rows.sort(key=lambda row: (-int(row["kurisu"]), str(row["path"])))
    return {
        "files_scanned": len(files),
        "speaker_totals": dict(sorted(speaker_totals.items())),
        "kurisu_candidates": total_kurisu,
        "amadeus_labeled_records": total_amadeus,
        "kurisu_near_amadeus_literal": total_kurisu_near_amadeus,
        "kurisu_near_digital_cues": total_kurisu_near_digital,
        "kurisu_near_flashback_cues": total_kurisu_near_flashback,
        "context_records": context_records,
        "files": rows,
    }


def main() -> int:
    args = _parser().parse_args()
    report = inspect(args.txt_dir, context_records=args.context_records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "SG0 identity diagnostic: "
        f"Kurisu={report['kurisu_candidates']} "
        f"Amadeus-label={report['amadeus_labeled_records']} "
        f"files={report['files_scanned']}"
    )
    print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
