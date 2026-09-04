from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .canon import CanonExample
from .context import CHARACTER_PROMPT_VERSION
from .persona_evaluation import (
    PersonaEvaluationObservation,
    PersonaEvaluationPolicyPlan,
    PersonaEvaluationReport,
    PersonaEvaluationRunner,
)

PERSONA_EVALUATION_CHECKPOINT_VERSION = 2
PERSONA_EVALUATION_RUN_VERSION = "persona-evaluation-run-v2-paired-policy"
PersonaEvaluationProgress = Callable[[int, int, str, str], None]


@dataclass(frozen=True, slots=True)
class PersonaEvaluationRunStats:
    total_observations: int
    resumed_observations: int
    provider_observations: int


@dataclass(frozen=True, slots=True)
class _CheckpointPolicyPlan:
    pair_id: str
    case_id: str
    plan: PersonaEvaluationPolicyPlan


@dataclass(frozen=True, slots=True)
class _PersonaEvaluationCheckpoint:
    input_fingerprint: str
    policies: tuple[_CheckpointPolicyPlan, ...]
    observations: tuple[PersonaEvaluationObservation, ...]


async def run_persona_evaluation_with_checkpoint(
    runner: PersonaEvaluationRunner,
    *,
    checkpoint_path: str | Path,
    progress: PersonaEvaluationProgress | None = None,
) -> tuple[PersonaEvaluationReport, PersonaEvaluationRunStats]:
    """Run the fixed matrix while checkpointing paired policy plans and observations.

    The policy for one Persona/case pair is persisted before either Canon variant is generated.
    If a run is interrupted after one side of the A/B pair completes, the resumed side therefore
    reuses the exact same policy instead of resampling the policy provider.
    """

    variant_groups = runner._variant_groups()  # noqa: SLF001
    expected_order = runner._expected_observation_order()  # noqa: SLF001
    expected_keys = set(expected_order)
    expected_policy_order = tuple(
        (pair_id, case.case_id)
        for pair_id, _persona, _pair_variants in variant_groups
        for case in runner._suite.cases  # noqa: SLF001
    )
    expected_policy_keys = set(expected_policy_order)
    fingerprint = _input_fingerprint(runner)
    checkpoint = _load_checkpoint(checkpoint_path, expected_fingerprint=fingerprint)

    completed: dict[tuple[str, str], PersonaEvaluationObservation] = {}
    for observation in checkpoint.observations:
        key = (observation.variant, observation.case_id)
        if key not in expected_keys:
            raise ValueError(
                "persona evaluation checkpoint contains an observation outside the current matrix: "
                f"{key}"
            )
        if key in completed:
            raise ValueError(f"persona evaluation checkpoint contains duplicate observation: {key}")
        completed[key] = observation

    policies: dict[tuple[str, str], PersonaEvaluationPolicyPlan] = {}
    for item in checkpoint.policies:
        key = (item.pair_id, item.case_id)
        if key not in expected_policy_keys:
            raise ValueError(
                "persona evaluation checkpoint contains a policy outside the current matrix: "
                f"{key}"
            )
        if key in policies:
            raise ValueError(f"persona evaluation checkpoint contains duplicate policy: {key}")
        policies[key] = item.plan

    resumed_observations = len(completed)
    provider_observations = 0

    for pair_id, persona, pair_variants in variant_groups:
        planner = runner._build_policy_planner(persona)  # noqa: SLF001
        for case in runner._suite.cases:  # noqa: SLF001
            policy_key = (pair_id, case.case_id)
            plan = policies.get(policy_key)
            if plan is None:
                plan = await runner._plan_policy(planner, case)  # noqa: SLF001
                policies[policy_key] = plan
                _write_checkpoint(
                    checkpoint_path,
                    _checkpoint_snapshot(
                        fingerprint=fingerprint,
                        policy_order=expected_policy_order,
                        policies=policies,
                        observation_order=expected_order,
                        observations=completed,
                    ),
                )

            for variant, canon_enabled in pair_variants:
                key = (variant, case.case_id)
                if key in completed:
                    continue

                observation = await runner._generate_observation(  # noqa: SLF001
                    variant=variant,
                    persona=persona,
                    canon_enabled=canon_enabled,
                    case=case,
                    plan=plan,
                )
                completed[key] = observation
                provider_observations += 1
                _write_checkpoint(
                    checkpoint_path,
                    _checkpoint_snapshot(
                        fingerprint=fingerprint,
                        policy_order=expected_policy_order,
                        policies=policies,
                        observation_order=expected_order,
                        observations=completed,
                    ),
                )
                if progress is not None:
                    progress(len(completed), len(expected_order), variant, case.case_id)

    ordered_observations = tuple(completed[key] for key in expected_order)
    return (
        runner._report(ordered_observations),  # noqa: SLF001
        PersonaEvaluationRunStats(
            total_observations=len(expected_order),
            resumed_observations=resumed_observations,
            provider_observations=provider_observations,
        ),
    )


def remove_persona_evaluation_checkpoint(path: str | Path) -> None:
    Path(path).unlink(missing_ok=True)


def _checkpoint_snapshot(
    *,
    fingerprint: str,
    policy_order: tuple[tuple[str, str], ...],
    policies: dict[tuple[str, str], PersonaEvaluationPolicyPlan],
    observation_order: tuple[tuple[str, str], ...],
    observations: dict[tuple[str, str], PersonaEvaluationObservation],
) -> _PersonaEvaluationCheckpoint:
    return _PersonaEvaluationCheckpoint(
        input_fingerprint=fingerprint,
        policies=tuple(
            _CheckpointPolicyPlan(pair_id=pair_id, case_id=case_id, plan=policies[key])
            for key in policy_order
            if key in policies
            for pair_id, case_id in (key,)
        ),
        observations=tuple(
            observations[key] for key in observation_order if key in observations
        ),
    )


def _input_fingerprint(runner: PersonaEvaluationRunner) -> str:
    digest = hashlib.sha256()
    candidate = runner._candidate  # noqa: SLF001
    candidate_persona = runner._candidate_persona  # noqa: SLF001
    header = {
        "run_version": PERSONA_EVALUATION_RUN_VERSION,
        "character_prompt_version": CHARACTER_PROMPT_VERSION,
        "model": runner._model,  # noqa: SLF001
        "suite": runner._suite.model_dump(mode="json"),  # noqa: SLF001
        "base_persona": runner._base_persona.core.model_dump(mode="json"),  # noqa: SLF001
        "candidate": candidate.model_dump(mode="json") if candidate is not None else None,
        "candidate_persona": (
            candidate_persona.core.model_dump(mode="json")
            if candidate_persona is not None
            else None
        ),
    }
    digest.update(
        json.dumps(
            header,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    digest.update(b"\n")
    for example in runner._canon_examples:  # noqa: SLF001
        digest.update(_canon_fingerprint_payload(example).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _canon_fingerprint_payload(example: CanonExample) -> str:
    return json.dumps(
        {
            "id": example.example_id,
            "source": example.source,
            "persona": example.persona,
            "history": list(example.history),
            "response": example.response,
            "act": example.act,
            "tags": list(example.tags),
            "search_summary": example.search_summary,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_checkpoint(
    path: str | Path,
    *,
    expected_fingerprint: str,
) -> _PersonaEvaluationCheckpoint:
    checkpoint_path = Path(path)
    if not checkpoint_path.exists():
        return _PersonaEvaluationCheckpoint(
            input_fingerprint=expected_fingerprint,
            policies=(),
            observations=(),
        )

    try:
        decoded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid persona evaluation checkpoint JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("persona evaluation checkpoint must be a JSON object")
    record = cast(dict[str, object], decoded)
    if record.get("version") != PERSONA_EVALUATION_CHECKPOINT_VERSION:
        raise ValueError(
            "unsupported persona evaluation checkpoint version: "
            f"{record.get('version')}"
        )
    fingerprint = record.get("input_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("persona evaluation checkpoint input_fingerprint must be a string")
    if fingerprint != expected_fingerprint:
        raise ValueError(
            "persona evaluation checkpoint does not match the current "
            "suite/persona/candidate/canon/model"
        )

    raw_policies = record.get("policies")
    if not isinstance(raw_policies, list):
        raise ValueError("persona evaluation checkpoint policies must be a list")
    policies: list[_CheckpointPolicyPlan] = []
    for item in raw_policies:
        if not isinstance(item, dict):
            raise ValueError("persona evaluation checkpoint policy must be an object")
        pair_id = item.get("pair_id")
        case_id = item.get("case_id")
        raw_plan = item.get("plan")
        if not isinstance(pair_id, str) or not pair_id:
            raise ValueError("persona evaluation checkpoint policy pair_id must be a string")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("persona evaluation checkpoint policy case_id must be a string")
        policies.append(
            _CheckpointPolicyPlan(
                pair_id=pair_id,
                case_id=case_id,
                plan=PersonaEvaluationPolicyPlan.model_validate(raw_plan),
            )
        )

    raw_observations = record.get("observations")
    if not isinstance(raw_observations, list):
        raise ValueError("persona evaluation checkpoint observations must be a list")
    observations = tuple(
        PersonaEvaluationObservation.model_validate(item) for item in raw_observations
    )
    return _PersonaEvaluationCheckpoint(
        input_fingerprint=fingerprint,
        policies=tuple(policies),
        observations=observations,
    )


def _write_checkpoint(
    path: str | Path,
    checkpoint: _PersonaEvaluationCheckpoint,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": PERSONA_EVALUATION_CHECKPOINT_VERSION,
                "input_fingerprint": checkpoint.input_fingerprint,
                "policies": [
                    {
                        "pair_id": item.pair_id,
                        "case_id": item.case_id,
                        "plan": item.plan.model_dump(mode="json"),
                    }
                    for item in checkpoint.policies
                ],
                "observations": [
                    item.model_dump(mode="json") for item in checkpoint.observations
                ],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
