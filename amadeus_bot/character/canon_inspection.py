from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from amadeus_bot.character.canon import CanonExample, load_canon_examples


@dataclass(frozen=True, slots=True)
class CanonInputReport:
    path: str
    examples: int


@dataclass(frozen=True, slots=True)
class CanonCorpusReport:
    inputs: tuple[CanonInputReport, ...]
    total_examples: int
    counts_by_source: dict[str, int]
    counts_by_persona: dict[str, int]
    counts_by_source_persona: dict[str, int]
    duplicate_ids: tuple[str, ...]
    id_prefix_mismatches: tuple[str, ...]
    examples_with_history: int
    max_history_entries: int
    examples_with_search_summary: int
    examples_with_act: int
    examples_with_tags: int

    @property
    def ok(self) -> bool:
        return not self.duplicate_ids and not self.id_prefix_mismatches

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_canon_corpus(paths: tuple[str | Path, ...]) -> CanonCorpusReport:
    """Inspect one or more local Canon JSONL files before costly offline processing.

    Each input is validated by the normal Canon loader. The combined inspection additionally checks
    duplicate ids across files and verifies that generated ids retain their source/persona prefix.
    """

    if not paths:
        raise ValueError("at least one canon input path is required")

    inputs: list[CanonInputReport] = []
    all_examples: list[CanonExample] = []
    for raw_path in paths:
        path = Path(raw_path)
        examples = load_canon_examples(path)
        inputs.append(CanonInputReport(path=str(path), examples=len(examples)))
        all_examples.extend(examples)

    source_counts: Counter[str] = Counter()
    persona_counts: Counter[str] = Counter()
    source_persona_counts: Counter[str] = Counter()
    id_counts: Counter[str] = Counter()
    id_prefix_mismatches: list[str] = []
    examples_with_history = 0
    max_history_entries = 0
    examples_with_search_summary = 0
    examples_with_act = 0
    examples_with_tags = 0

    for example in all_examples:
        source_counts[example.source] += 1
        persona_counts[example.persona] += 1
        source_persona_counts[f"{example.source}/{example.persona}"] += 1
        id_counts[example.example_id] += 1

        expected_prefix = f"{example.source}:{example.persona}:"
        if not example.example_id.startswith(expected_prefix):
            id_prefix_mismatches.append(example.example_id)

        if example.history:
            examples_with_history += 1
            max_history_entries = max(max_history_entries, len(example.history))
        if example.search_summary:
            examples_with_search_summary += 1
        if example.act:
            examples_with_act += 1
        if example.tags:
            examples_with_tags += 1

    duplicate_ids = tuple(
        sorted(example_id for example_id, count in id_counts.items() if count > 1)
    )

    return CanonCorpusReport(
        inputs=tuple(inputs),
        total_examples=len(all_examples),
        counts_by_source=dict(sorted(source_counts.items())),
        counts_by_persona=dict(sorted(persona_counts.items())),
        counts_by_source_persona=dict(sorted(source_persona_counts.items())),
        duplicate_ids=duplicate_ids,
        id_prefix_mismatches=tuple(sorted(id_prefix_mismatches)),
        examples_with_history=examples_with_history,
        max_history_entries=max_history_entries,
        examples_with_search_summary=examples_with_search_summary,
        examples_with_act=examples_with_act,
        examples_with_tags=examples_with_tags,
    )
