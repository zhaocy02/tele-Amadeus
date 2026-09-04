from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .canon import CanonExample
from .canon_distillation import (
    CANON_DISTILLATION_PROMPT_VERSION,
    CanonPersonaDistiller,
    DistilledPersonaRule,
    PersonaDistillationCandidate,
    PersonaDistillationConfig,
)
from .persona import PersonaCore

PERSONA_DISTILLATION_CHECKPOINT_VERSION = 1

_CheckpointPhase = Literal["extract", "consolidate"]


@dataclass(frozen=True, slots=True)
class PersonaDistillationRunStats:
    total_examples: int
    total_extraction_batches: int
    resumed_extraction_batches: int
    provider_extraction_batches: int
    consolidation_passes_this_run: int


@dataclass(frozen=True, slots=True)
class _PersonaDistillationCheckpoint:
    input_fingerprint: str
    phase: _CheckpointPhase
    next_extract_batch: int
    rules: tuple[DistilledPersonaRule, ...]
    completed_consolidation_passes: int


async def distill_persona_with_checkpoint(
    examples: tuple[CanonExample, ...],
    *,
    distiller: CanonPersonaDistiller,
    current_persona: PersonaCore,
    config: PersonaDistillationConfig,
    checkpoint_path: str | Path,
) -> tuple[PersonaDistillationCandidate, PersonaDistillationRunStats]:
    """Run persona distillation while preserving completed extraction work.

    Extraction batches are checkpointed individually. Consolidation is checkpointed after each
    completed pass, so an interruption can at worst repeat the current consolidation pass rather
    than all prior extraction calls.
    """

    usable = tuple(example for example in examples if example.search_summary.strip())
    if len(usable) < 2:
        return (
            PersonaDistillationCandidate(
                source_example_count=len(usable),
                current_persona_version=current_persona.persona_version,
                rules=(),
            ),
            PersonaDistillationRunStats(
                total_examples=len(usable),
                total_extraction_batches=0,
                resumed_extraction_batches=0,
                provider_extraction_batches=0,
                consolidation_passes_this_run=0,
            ),
        )

    batches = _extraction_batches(usable, batch_size=config.batch_size)
    fingerprint = _input_fingerprint(
        usable,
        current_persona=current_persona,
        config=config,
    )
    checkpoint = _load_checkpoint(checkpoint_path, expected_fingerprint=fingerprint)
    if checkpoint.next_extract_batch > len(batches):
        raise ValueError("persona distillation checkpoint is past the current extraction plan")

    resumed_extraction_batches = checkpoint.next_extract_batch
    provider_extraction_batches = 0
    rules = list(checkpoint.rules)
    phase = checkpoint.phase
    next_extract_batch = checkpoint.next_extract_batch
    completed_consolidation_passes = checkpoint.completed_consolidation_passes

    if phase == "extract":
        for batch_index in range(next_extract_batch, len(batches)):
            extracted = await distiller._extract_rules(  # noqa: SLF001
                batches[batch_index],
                current_persona=current_persona,
            )
            rules.extend(extracted)
            provider_extraction_batches += 1
            next_extract_batch = batch_index + 1
            _write_checkpoint(
                checkpoint_path,
                _PersonaDistillationCheckpoint(
                    input_fingerprint=fingerprint,
                    phase="extract" if next_extract_batch < len(batches) else "consolidate",
                    next_extract_batch=next_extract_batch,
                    rules=tuple(rules),
                    completed_consolidation_passes=completed_consolidation_passes,
                ),
            )
        phase = "consolidate"

    if phase != "consolidate":
        raise RuntimeError(f"unsupported persona distillation checkpoint phase: {phase}")

    consolidation_passes_this_run = 0
    current_rules = tuple(rules)
    while len(current_rules) > config.consolidation_batch_size:
        previous_count = len(current_rules)
        next_pass: list[DistilledPersonaRule] = []
        for start in range(0, len(current_rules), config.consolidation_batch_size):
            chunk = current_rules[start : start + config.consolidation_batch_size]
            next_pass.extend(
                await distiller._consolidate_rules(chunk, examples=usable)  # noqa: SLF001
            )
        consolidation_passes_this_run += 1
        completed_consolidation_passes += 1
        if len(next_pass) >= previous_count:
            current_rules = tuple(next_pass[: config.consolidation_batch_size])
        else:
            current_rules = tuple(next_pass)
        _write_checkpoint(
            checkpoint_path,
            _PersonaDistillationCheckpoint(
                input_fingerprint=fingerprint,
                phase="consolidate",
                next_extract_batch=len(batches),
                rules=current_rules,
                completed_consolidation_passes=completed_consolidation_passes,
            ),
        )
        if len(next_pass) >= previous_count:
            break

    if len(current_rules) > 1:
        current_rules = await distiller._consolidate_rules(  # noqa: SLF001
            current_rules,
            examples=usable,
        )
        consolidation_passes_this_run += 1
        completed_consolidation_passes += 1
        _write_checkpoint(
            checkpoint_path,
            _PersonaDistillationCheckpoint(
                input_fingerprint=fingerprint,
                phase="consolidate",
                next_extract_batch=len(batches),
                rules=current_rules,
                completed_consolidation_passes=completed_consolidation_passes,
            ),
        )

    candidate = PersonaDistillationCandidate(
        source_example_count=len(usable),
        current_persona_version=current_persona.persona_version,
        rules=distiller._stable_sort(current_rules),  # noqa: SLF001
    )
    return (
        candidate,
        PersonaDistillationRunStats(
            total_examples=len(usable),
            total_extraction_batches=len(batches),
            resumed_extraction_batches=resumed_extraction_batches,
            provider_extraction_batches=provider_extraction_batches,
            consolidation_passes_this_run=consolidation_passes_this_run,
        ),
    )


def remove_persona_distillation_checkpoint(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def _extraction_batches(
    examples: tuple[CanonExample, ...],
    *,
    batch_size: int,
) -> tuple[tuple[CanonExample, ...], ...]:
    groups: dict[tuple[str, str], list[CanonExample]] = {}
    for example in examples:
        groups.setdefault((example.source, example.persona), []).append(example)

    batches: list[tuple[CanonExample, ...]] = []
    for key in sorted(groups):
        group = tuple(groups[key])
        for start in range(0, len(group), batch_size):
            batches.append(group[start : start + batch_size])
    return tuple(batches)


def _input_fingerprint(
    examples: tuple[CanonExample, ...],
    *,
    current_persona: PersonaCore,
    config: PersonaDistillationConfig,
) -> str:
    digest = hashlib.sha256()
    header = {
        "prompt_version": CANON_DISTILLATION_PROMPT_VERSION,
        "persona_version": current_persona.persona_version,
        "config": {
            "batch_size": config.batch_size,
            "max_rules_per_batch": config.max_rules_per_batch,
            "consolidation_batch_size": config.consolidation_batch_size,
            "max_rules_per_consolidation": config.max_rules_per_consolidation,
        },
    }
    digest.update(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")
    for example in examples:
        payload = {
            "id": example.example_id,
            "source": example.source,
            "persona": example.persona,
            "act": example.act,
            "tags": list(example.tags),
            "search_summary": example.search_summary,
        }
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _load_checkpoint(
    path: str | Path,
    *,
    expected_fingerprint: str,
) -> _PersonaDistillationCheckpoint:
    checkpoint_path = Path(path)
    if not checkpoint_path.exists():
        return _PersonaDistillationCheckpoint(
            input_fingerprint=expected_fingerprint,
            phase="extract",
            next_extract_batch=0,
            rules=(),
            completed_consolidation_passes=0,
        )

    try:
        decoded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid persona distillation checkpoint JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("persona distillation checkpoint must be a JSON object")
    record = cast(dict[str, object], decoded)
    if record.get("version") != PERSONA_DISTILLATION_CHECKPOINT_VERSION:
        raise ValueError(
            "unsupported persona distillation checkpoint version: "
            f"{record.get('version')}"
        )
    fingerprint = _required_string(record.get("input_fingerprint"), "input_fingerprint")
    if fingerprint != expected_fingerprint:
        raise ValueError(
            "persona distillation checkpoint does not match the current input/persona/config; "
            "remove it before starting a different build"
        )
    phase_raw = _required_string(record.get("phase"), "phase")
    if phase_raw not in {"extract", "consolidate"}:
        raise ValueError(f"invalid persona distillation checkpoint phase: {phase_raw}")
    phase = cast(_CheckpointPhase, phase_raw)
    next_extract_batch = record.get("next_extract_batch")
    if not isinstance(next_extract_batch, int) or next_extract_batch < 0:
        raise ValueError("persona distillation checkpoint next_extract_batch must be >= 0")
    completed_passes = record.get("completed_consolidation_passes", 0)
    if not isinstance(completed_passes, int) or completed_passes < 0:
        raise ValueError(
            "persona distillation checkpoint completed_consolidation_passes must be >= 0"
        )
    raw_rules = record.get("rules")
    if not isinstance(raw_rules, list):
        raise ValueError("persona distillation checkpoint rules must be a list")
    rules = tuple(
        DistilledPersonaRule.model_validate(cast(dict[str, object], item))
        for item in cast(list[object], raw_rules)
        if isinstance(item, dict)
    )
    if len(rules) != len(raw_rules):
        raise ValueError("persona distillation checkpoint rules must be JSON objects")
    return _PersonaDistillationCheckpoint(
        input_fingerprint=fingerprint,
        phase=phase,
        next_extract_batch=next_extract_batch,
        rules=rules,
        completed_consolidation_passes=completed_passes,
    )


def _write_checkpoint(
    path: str | Path,
    checkpoint: _PersonaDistillationCheckpoint,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": PERSONA_DISTILLATION_CHECKPOINT_VERSION,
                "input_fingerprint": checkpoint.input_fingerprint,
                "phase": checkpoint.phase,
                "next_extract_batch": checkpoint.next_extract_batch,
                "completed_consolidation_passes": checkpoint.completed_consolidation_passes,
                "rules": [rule.model_dump(mode="json") for rule in checkpoint.rules],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"persona distillation checkpoint {field} must be a non-empty string")
    return value.strip()
