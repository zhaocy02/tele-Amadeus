from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SPEAKER_LINE_RE = re.compile(r"^([^:\n]{1,80}):(?:\s*(.*))$")
_SC3_SPOKEN_RE = re.compile(r"\[name\](.*?)\[line\](.*)", re.IGNORECASE | re.DOTALL)
_SC3_TERMINATOR_RE = re.compile(r"\[%[^\]]+\]")
_SC3_RECORD_RE = re.compile(r".*?\[%[^\]]+\]", re.DOTALL)
_SC3_TAG_RE = re.compile(r"\[[^\]]+\]")
_SCENE_BREAK_SPEAKER = "__SCENE_BREAK__"

InputFormat = Literal["speaker", "sc3"]


@dataclass(frozen=True, slots=True)
class Utterance:
    speaker: str
    text: str


def parse_speaker_dialogue(text: str) -> tuple[Utterance, ...]:
    """Parse simple `Speaker: text` dialogue while preserving continuation lines."""

    utterances: list[Utterance] = []
    current_speaker = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_speaker, current_lines
        joined = " ".join(part.strip() for part in current_lines if part.strip()).strip()
        if current_speaker and joined:
            utterances.append(Utterance(speaker=current_speaker, text=joined))
        current_speaker = ""
        current_lines = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _SPEAKER_LINE_RE.match(line)
        if match is not None:
            flush()
            current_speaker = match.group(1).strip()
            tail = match.group(2).strip()
            current_lines = [tail] if tail else []
            continue
        if current_speaker:
            current_lines.append(line)

    flush()
    return tuple(utterances)


def parse_sc3_dialogue(text: str) -> tuple[Utterance, ...]:
    """Parse sc3tools `.scx.txt` output into compact speaker/dialogue utterances.

    MAGES/sc3tools text uses records such as ``[name]Kurisu[line]...[%p]``. A single text
    record may contain physical newlines before its ``[%...]`` terminator, so parsing is based on
    MAGES record terminators rather than ``splitlines()``. This keeps multiline dialogue intact,
    strips engine/control tags, and merges consecutive records from the same speaker.
    """

    utterances: list[Utterance] = []
    for raw_record in _iter_sc3_records(text):
        record = raw_record.strip()
        if not record:
            continue

        spoken = _SC3_SPOKEN_RE.search(record)
        if spoken is not None:
            speaker = _clean_sc3_text(spoken.group(1))
            dialogue = _before_sc3_terminator(spoken.group(2))
            dialogue = _clean_sc3_text(dialogue)
            if speaker and dialogue:
                utterances.append(Utterance(speaker=speaker, text=dialogue))
            continue

        narration = _clean_sc3_text(_before_sc3_terminator(record))
        if narration:
            utterances.append(Utterance(speaker="UNSPOKEN", text=narration))

    return merge_consecutive_utterances(tuple(utterances))


def load_dialogue_source(
    path: str | Path,
    *,
    input_format: InputFormat,
) -> tuple[Utterance, ...]:
    """Load one dialogue source file or a directory of sc3tools text files.

    Directory input is intentionally limited to sc3tools output. Each file is parsed independently
    and separated explicitly so neither utterance merging nor history windows cross script files.
    """

    source_path = Path(path)
    if source_path.is_file():
        text = source_path.read_text(encoding="utf-8")
        return parse_sc3_dialogue(text) if input_format == "sc3" else parse_speaker_dialogue(text)

    if not source_path.is_dir():
        raise ValueError(f"input path does not exist: {source_path}")
    if input_format != "sc3":
        raise ValueError("directory input is supported only with input_format='sc3'")

    files = tuple(
        sorted(
            item
            for item in source_path.rglob("*")
            if item.is_file() and item.suffix.casefold() == ".txt"
        )
    )
    if not files:
        raise ValueError(f"no .txt files found under sc3 input directory: {source_path}")

    utterances: list[Utterance] = []
    for file_path in files:
        parsed = parse_sc3_dialogue(file_path.read_text(encoding="utf-8"))
        if utterances and parsed:
            utterances.append(Utterance(speaker=_SCENE_BREAK_SPEAKER, text=""))
        utterances.extend(parsed)
    return tuple(utterances)


def merge_consecutive_utterances(
    utterances: tuple[Utterance, ...],
) -> tuple[Utterance, ...]:
    """Join adjacent text boxes from the same speaker without crossing scene/file boundaries."""

    merged: list[Utterance] = []
    for utterance in utterances:
        if merged and merged[-1].speaker == utterance.speaker:
            previous = merged[-1]
            merged[-1] = Utterance(
                speaker=previous.speaker,
                text=f"{previous.text} {utterance.text}".strip(),
            )
        else:
            merged.append(utterance)
    return tuple(merged)


def build_records(
    utterances: tuple[Utterance, ...],
    *,
    source: str,
    persona: str,
    speaker: str | tuple[str, ...],
    history_size: int,
) -> tuple[dict[str, object], ...]:
    if history_size < 0:
        raise ValueError("history_size must not be negative")

    speaker_names = (speaker,) if isinstance(speaker, str) else speaker
    targets = {name.strip().casefold() for name in speaker_names if name.strip()}
    records: list[dict[str, object]] = []
    target_index = 0
    for index, utterance in enumerate(utterances):
        if utterance.speaker.casefold() not in targets:
            continue
        history = _history_before(utterances, index=index, history_size=history_size)
        records.append(
            {
                "id": f"{source}:{persona}:{target_index:06d}",
                "source": source,
                "persona": persona,
                "history": history,
                "response": utterance.text,
            }
        )
        target_index += 1
    return tuple(records)


def write_jsonl(records: tuple[dict[str, object], ...], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def _history_before(
    utterances: tuple[Utterance, ...],
    *,
    index: int,
    history_size: int,
) -> list[str]:
    if history_size == 0:
        return []

    history: list[str] = []
    cursor = index - 1
    while cursor >= 0 and len(history) < history_size:
        previous = utterances[cursor]
        if previous.speaker == _SCENE_BREAK_SPEAKER:
            break
        if previous.text:
            history.append(f"{previous.speaker}: {previous.text}")
        cursor -= 1
    history.reverse()
    return history


def _iter_sc3_records(text: str) -> tuple[str, ...]:
    """Split sc3tools text on MAGES presentation terminators, preserving embedded newlines."""

    records: list[str] = []
    cursor = 0
    for match in _SC3_RECORD_RE.finditer(text):
        records.append(match.group(0))
        cursor = match.end()

    trailing = text[cursor:].strip()
    if trailing:
        records.append(trailing)
    return tuple(records)


def _before_sc3_terminator(text: str) -> str:
    parts = _SC3_TERMINATOR_RE.split(text, maxsplit=1)
    return parts[0] if parts else text


def _clean_sc3_text(text: str) -> str:
    without_tags = _SC3_TAG_RE.sub("", text)
    normalized = " ".join(without_tags.split()).strip()
    return normalized.strip("“”").strip()
