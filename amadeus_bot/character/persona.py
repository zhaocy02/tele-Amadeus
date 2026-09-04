from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class PersonaCoreLoadError(ValueError):
    """Raised when a versioned Persona Core cannot be loaded or validated."""


class PersonaTrigger(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    cues: tuple[str, ...]
    behavior: str

    @field_validator("label", "behavior")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("persona trigger text must not be empty")
        return cleaned

    @field_validator("cues")
    @classmethod
    def validate_cues(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value if item.strip())
        if not cleaned:
            raise ValueError("persona trigger must contain at least one cue")
        return cleaned


class PersonaCore(BaseModel):
    """Stable, versioned character identity that excludes mutable conversation state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    persona_version: str
    identity: tuple[str, ...]
    core_values: tuple[str, ...]
    speech_tendencies: tuple[str, ...]
    social_behavior: tuple[str, ...]
    relationship_baseline: tuple[str, ...]
    triggers: tuple[PersonaTrigger, ...]
    anti_assistant_patterns: tuple[str, ...]
    fact_boundary: tuple[str, ...]

    @field_validator(
        "persona_version",
        "identity",
        "core_values",
        "speech_tendencies",
        "social_behavior",
        "relationship_baseline",
        "anti_assistant_patterns",
        "fact_boundary",
    )
    @classmethod
    def validate_nonempty(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise ValueError("persona_version must not be empty")
            return cleaned
        cleaned_items = tuple(item.strip() for item in value if item.strip())
        if not cleaned_items:
            raise ValueError("persona sections must not be empty")
        return cleaned_items

    def render_character_prompt(self) -> str:
        """Render stable Persona Core with explicit source boundaries for the Character LLM."""

        sections = (
            ("IDENTITY", self.identity),
            ("CORE VALUES", self.core_values),
            ("SPEECH TENDENCIES", self.speech_tendencies),
            ("SOCIAL BEHAVIOR", self.social_behavior),
            ("RELATIONSHIP BASELINE", self.relationship_baseline),
            (
                "TRIGGERS",
                tuple(
                    f"{trigger.label}: cues={', '.join(trigger.cues)}; {trigger.behavior}"
                    for trigger in self.triggers
                ),
            ),
            ("ANTI-ASSISTANT", self.anti_assistant_patterns),
            ("FACT BOUNDARY", self.fact_boundary),
        )
        rendered = [f"# Persona Core v2 ({self.persona_version})"]
        for heading, items in sections:
            rendered.append(f"[{heading}]\n" + "\n".join(f"- {item}" for item in items))
        return "\n\n".join(rendered)

    def render_policy_context(self) -> str:
        """Render the subset useful to the hidden Conversation Policy planner."""

        trigger_lines = tuple(
            f"- {trigger.label}: {trigger.behavior} (cues: {', '.join(trigger.cues)})"
            for trigger in self.triggers
        )
        return "\n\n".join(
            (
                "[CORE VALUES]\n" + "\n".join(f"- {item}" for item in self.core_values),
                "[SOCIAL BEHAVIOR]\n"
                + "\n".join(f"- {item}" for item in self.social_behavior),
                "[RELATIONSHIP BASELINE]\n"
                + "\n".join(f"- {item}" for item in self.relationship_baseline),
                "[TRIGGERS]\n" + "\n".join(trigger_lines),
                "[ANTI-ASSISTANT]\n"
                + "\n".join(f"- {item}" for item in self.anti_assistant_patterns),
            )
        )


@dataclass(frozen=True, slots=True)
class LoadedPersonaCore:
    core: PersonaCore
    version_hash: str
    source_path: Path


def load_persona_core(path: Path) -> LoadedPersonaCore:
    """Load and hash a version-controlled Persona Core JSON document."""

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaCoreLoadError(f"unable to read Persona Core: {path}") from exc

    try:
        raw: object = json.loads(raw_text)
        core = PersonaCore.model_validate(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PersonaCoreLoadError(f"invalid Persona Core: {path}") from exc

    canonical = json.dumps(
        core.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    version_hash = hashlib.sha256(canonical).hexdigest()
    return LoadedPersonaCore(core=core, version_hash=version_hash, source_path=path.resolve())
