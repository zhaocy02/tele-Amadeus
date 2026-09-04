from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

LEGACY_PERSONA_FILES = (
    "SOUL.md",
    "LORE.md",
    "STYLE.md",
    "EXAMPLES.md",
    "MEMORY_POLICY.md",
)


class PersonaLoadError(RuntimeError):
    """Raised when the version-controlled persona baseline is incomplete."""


@dataclass(frozen=True, slots=True)
class LegacyPersona:
    instructions: str
    version_hash: str
    source_files: tuple[str, ...] = LEGACY_PERSONA_FILES

    def with_memory_context(self, memory_context: str) -> str:
        context = memory_context.strip()
        if not context:
            return self.instructions
        return f"{self.instructions}\n\n{context}"


def load_legacy_persona(directory: Path) -> LegacyPersona:
    sections: list[str] = []
    digest = hashlib.sha256()
    for filename in LEGACY_PERSONA_FILES:
        path = directory / filename
        if not path.is_file():
            raise PersonaLoadError(f"missing legacy persona file: {filename}")
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise PersonaLoadError(f"legacy persona file is empty: {filename}")
        sections.append(text)
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
    return LegacyPersona(instructions="\n\n".join(sections), version_hash=digest.hexdigest())
