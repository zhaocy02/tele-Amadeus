from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from amadeus_bot.llm import LLMMessage, LLMProvider, MessageRole

from .canon import CanonExample, CanonRetriever, render_canon_reference
from .canon_distillation import PersonaDistillationCandidate
from .context import CharacterContextBuilder, CharacterContextSources
from .generator import CharacterGenerator
from .persona import LoadedPersonaCore, PersonaCore
from .policy import ConversationPolicy, ConversationPolicyPlanner, PolicyContext
from .turn_engine import CharacterTurnInput


class PersonaEvaluationMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("evaluation message content must not be empty")
        return cleaned


class PersonaEvaluationCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    category: str
    user_message: str
    recent_conversation: tuple[PersonaEvaluationMessage, ...] = ()
    expected_traits: tuple[str, ...]
    avoid_traits: tuple[str, ...] = ()
    requires_full_answer: bool = False

    @field_validator("case_id", "category", "user_message")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("evaluation case text must not be empty")
        return cleaned

    @field_validator("expected_traits")
    @classmethod
    def validate_expected_traits(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value if item.strip())
        if not cleaned:
            raise ValueError("evaluation case requires at least one expected trait")
        return cleaned

    @field_validator("avoid_traits")
    @classmethod
    def validate_avoid_traits(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item.strip() for item in value if item.strip())


class PersonaEvaluationSuite(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    suite_version: str
    cases: tuple[PersonaEvaluationCase, ...]

    @field_validator("suite_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("evaluation suite version must not be empty")
        return cleaned

    @field_validator("cases")
    @classmethod
    def validate_cases(
        cls,
        value: tuple[PersonaEvaluationCase, ...],
    ) -> tuple[PersonaEvaluationCase, ...]:
        if not value:
            raise ValueError("evaluation suite must contain at least one case")
        ids = tuple(case.case_id for case in value)
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case ids must be unique")
        return value


class PersonaEvaluationPolicyPlan(BaseModel):
    """One policy decision reused by every Canon variant in an A/B pair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ConversationPolicy
    policy_mode: Literal["fast", "llm"]
    policy_ms: int = Field(ge=0)
    policy_fingerprint: str


class PersonaEvaluationObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    variant: str
    response: str
    policy_act: str
    policy_mode: Literal["fast", "llm"]
    policy_reason_label: str
    policy_answer_obligation: str
    policy_intensity: float = Field(ge=0.0, le=1.0)
    policy_state_bias: str
    policy_fingerprint: str
    canon_example_ids: tuple[str, ...] = ()
    persona_version: str
    persona_hash: str
    policy_ms: int = Field(ge=0)
    context_ms: int = Field(ge=0)
    generation_ms: int = Field(ge=0)
    total_ms: int = Field(ge=0)


class PersonaEvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    suite_version: str
    base_persona_version: str
    candidate_version: str | None = None
    canon_example_count: int = Field(ge=0)
    cases: tuple[PersonaEvaluationCase, ...]
    observations: tuple[PersonaEvaluationObservation, ...]


class PersonaEvaluationRunner:
    """Run inspectable Persona/RAG variants against a fixed evaluation suite.

    Variants that differ only by Canon availability share one Conversation Policy decision. This
    keeps the OFF/ON comparison attributable to Canon context instead of independently resampling
    a policy LLM for each side of the A/B pair.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        base_persona: LoadedPersonaCore,
        suite: PersonaEvaluationSuite,
        model: str | None = None,
        candidate: PersonaDistillationCandidate | None = None,
        candidate_persona: LoadedPersonaCore | None = None,
        canon_examples: tuple[CanonExample, ...] = (),
    ) -> None:
        if candidate is not None and candidate_persona is not None:
            raise ValueError("candidate and candidate_persona are mutually exclusive")
        self._provider = provider
        self._base_persona = base_persona
        self._suite = suite
        self._model = model
        self._candidate = candidate
        self._candidate_persona = candidate_persona
        self._canon_examples = canon_examples

    async def run(self) -> PersonaEvaluationReport:
        completed: dict[tuple[str, str], PersonaEvaluationObservation] = {}
        for _pair_id, persona, pair_variants in self._variant_groups():
            planner = self._build_policy_planner(persona)
            for case in self._suite.cases:
                plan = await self._plan_policy(planner, case)
                for variant, canon_enabled in pair_variants:
                    completed[(variant, case.case_id)] = await self._generate_observation(
                        variant=variant,
                        persona=persona,
                        canon_enabled=canon_enabled,
                        case=case,
                        plan=plan,
                    )

        return self._report(
            tuple(completed[key] for key in self._expected_observation_order())
        )

    def _report(
        self,
        observations: tuple[PersonaEvaluationObservation, ...],
    ) -> PersonaEvaluationReport:
        return PersonaEvaluationReport(
            suite_version=self._suite.suite_version,
            base_persona_version=self._base_persona.core.persona_version,
            candidate_version=self._candidate_version(),
            canon_example_count=len(self._canon_examples),
            cases=self._suite.cases,
            observations=observations,
        )

    def _candidate_version(self) -> str | None:
        if self._candidate_persona is not None:
            return self._candidate_persona.core.persona_version
        if self._candidate is not None:
            return self._candidate.candidate_version
        return None

    def _variant_groups(
        self,
    ) -> tuple[tuple[str, LoadedPersonaCore, tuple[tuple[str, bool], ...]], ...]:
        baseline_variants: list[tuple[str, bool]] = [("baseline_rag_off", False)]
        if self._canon_examples:
            baseline_variants.append(("baseline_rag_on", True))
        groups: list[
            tuple[str, LoadedPersonaCore, tuple[tuple[str, bool], ...]]
        ] = [("baseline", self._base_persona, tuple(baseline_variants))]

        candidate_persona = self._resolved_candidate_persona()
        if candidate_persona is not None:
            candidate_variants: list[tuple[str, bool]] = [("candidate_rag_off", False)]
            if self._canon_examples:
                candidate_variants.append(("candidate_rag_on", True))
            groups.append(("candidate", candidate_persona, tuple(candidate_variants)))
        return tuple(groups)

    def _variants(self) -> tuple[tuple[str, LoadedPersonaCore, bool], ...]:
        return tuple(
            (variant, persona, canon_enabled)
            for _pair_id, persona, pair_variants in self._variant_groups()
            for variant, canon_enabled in pair_variants
        )

    def _expected_observation_order(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (variant, case.case_id)
            for variant, _persona, _canon_enabled in self._variants()
            for case in self._suite.cases
        )

    def _resolved_candidate_persona(self) -> LoadedPersonaCore | None:
        if self._candidate_persona is not None:
            return self._candidate_persona
        if self._candidate is not None:
            return overlay_persona_candidate(self._base_persona, self._candidate)
        return None

    def _build_policy_planner(self, persona: LoadedPersonaCore) -> ConversationPolicyPlanner:
        return ConversationPolicyPlanner(
            provider=self._provider,
            persona=persona.core,
            model=self._model,
        )

    async def _plan_policy(
        self,
        planner: ConversationPolicyPlanner,
        case: PersonaEvaluationCase,
    ) -> PersonaEvaluationPolicyPlan:
        turn = self._turn_input(case)
        started = perf_counter()
        policy = await planner.plan(
            PolicyContext(
                user_message=turn.current_user_message,
                recent_conversation=self._policy_recent_conversation(turn.recent_conversation),
                requires_full_answer=turn.requires_full_answer,
            )
        )
        policy_ms = self._elapsed_ms(started)
        policy_mode: Literal["fast", "llm"] = (
            "fast" if policy.reason_label.startswith("fast_") else "llm"
        )
        return PersonaEvaluationPolicyPlan(
            policy=policy,
            policy_mode=policy_mode,
            policy_ms=policy_ms,
            policy_fingerprint=self._policy_fingerprint(policy),
        )

    async def _generate_observation(
        self,
        *,
        variant: str,
        persona: LoadedPersonaCore,
        canon_enabled: bool,
        case: PersonaEvaluationCase,
        plan: PersonaEvaluationPolicyPlan,
    ) -> PersonaEvaluationObservation:
        retriever = CanonRetriever(self._canon_examples) if canon_enabled else None
        turn = self._turn_input(case)

        context_started = perf_counter()
        canon_ids, canon_references = self._canon_material(
            retriever,
            query=case.user_message,
            act=plan.policy.act.value,
        )
        context = CharacterContextBuilder(persona).build(
            policy=plan.policy,
            sources=CharacterContextSources(
                current_user_message=turn.current_user_message,
                recent_conversation=turn.recent_conversation,
                canon_examples=canon_references,
            ),
        )
        context_ms = self._elapsed_ms(context_started)

        generation_started = perf_counter()
        response = await CharacterGenerator(
            provider=self._provider,
            model=self._model,
        ).generate(context)
        generation_ms = self._elapsed_ms(generation_started)

        policy = plan.policy
        return PersonaEvaluationObservation(
            case_id=case.case_id,
            variant=variant,
            response=response.text,
            policy_act=policy.act.value,
            policy_mode=plan.policy_mode,
            policy_reason_label=policy.reason_label,
            policy_answer_obligation=policy.answer_obligation.value,
            policy_intensity=policy.intensity,
            policy_state_bias=policy.state_bias,
            policy_fingerprint=plan.policy_fingerprint,
            canon_example_ids=canon_ids,
            persona_version=context.persona_version,
            persona_hash=context.persona_hash,
            policy_ms=plan.policy_ms,
            context_ms=context_ms,
            generation_ms=generation_ms,
            total_ms=plan.policy_ms + context_ms + generation_ms,
        )

    @staticmethod
    def _turn_input(case: PersonaEvaluationCase) -> CharacterTurnInput:
        role_map = {
            "user": MessageRole.USER,
            "assistant": MessageRole.ASSISTANT,
        }
        recent = tuple(
            LLMMessage(role=role_map[message.role], content=message.content)
            for message in case.recent_conversation
        )
        return CharacterTurnInput(
            current_user_message=case.user_message,
            recent_conversation=recent,
            requires_full_answer=case.requires_full_answer,
        )

    @staticmethod
    def _canon_material(
        retriever: CanonRetriever | None,
        *,
        query: str,
        act: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if retriever is None:
            return (), ()
        result = retriever.retrieve(query, act=act, limit=2)
        references = tuple(
            rendered
            for item in result.items
            if (rendered := render_canon_reference(item.example))
        )
        return result.example_ids, references

    @staticmethod
    def _policy_recent_conversation(messages: tuple[LLMMessage, ...]) -> tuple[str, ...]:
        return tuple(f"{message.role.value}: {message.content}" for message in messages[-6:])

    @staticmethod
    def _policy_fingerprint(policy: ConversationPolicy) -> str:
        canonical = json.dumps(
            policy.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int(round((perf_counter() - started) * 1000)))


def overlay_persona_candidate(
    base: LoadedPersonaCore,
    candidate: PersonaDistillationCandidate,
) -> LoadedPersonaCore:
    """Apply candidate rules in memory for evaluation without touching the Persona Core file."""

    updates: dict[str, object] = {}
    for rule in candidate.rules:
        existing = cast(tuple[str, ...], getattr(base.core, rule.section))
        if rule.behavior in existing:
            continue
        current = cast(tuple[str, ...], updates.get(rule.section, existing))
        updates[rule.section] = (*current, rule.behavior)

    fingerprint = hashlib.sha256(
        json.dumps(
            candidate.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:10]
    updates["persona_version"] = f"{base.core.persona_version}+eval.{fingerprint}"
    core = base.core.model_copy(update=updates)
    return _loaded_persona(core, source_path=base.source_path)


def load_persona_evaluation_suite(path: str | Path) -> PersonaEvaluationSuite:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        return PersonaEvaluationSuite.model_validate(raw)
    except OSError as exc:
        raise ValueError(f"unable to read persona evaluation suite: {source}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid persona evaluation suite: {source}") from exc


def load_persona_distillation_candidate(path: str | Path) -> PersonaDistillationCandidate:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        return PersonaDistillationCandidate.model_validate(raw)
    except OSError as exc:
        raise ValueError(f"unable to read persona distillation candidate: {source}") from exc
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid persona distillation candidate: {source}") from exc


def write_persona_evaluation_report(
    path: str | Path,
    report: PersonaEvaluationReport,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _loaded_persona(core: PersonaCore, *, source_path: Path) -> LoadedPersonaCore:
    canonical = json.dumps(
        core.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    version_hash = hashlib.sha256(canonical).hexdigest()
    return LoadedPersonaCore(core=core, version_hash=version_hash, source_path=source_path)
