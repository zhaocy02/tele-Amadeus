from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from amadeus_bot.memory import MigrationRehearsalReport, rehearse_legacy_v1_memory_migration

from .preferences import SQLiteRuntimePreferenceStore


@dataclass(frozen=True, slots=True)
class CutoverPreparationReport:
    migration: MigrationRehearsalReport
    memory_preference_applied: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


async def prepare_v2_cutover_data(
    *,
    source_snapshot: Path,
    target_data_dir: Path,
    chat_id: int,
) -> CutoverPreparationReport:
    """Prepare a brand-new v2 data directory from an explicit legacy snapshot.

    The caller is responsible for creating the snapshot. This function deliberately refuses to
    operate on a non-empty target so it can never overwrite an already-started v2 runtime.
    """

    source = source_snapshot.expanduser().resolve()
    target = target_data_dir.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"cutover source snapshot is not a file: {source}")
    if target.exists() and any(target.iterdir()):
        raise RuntimeError("cutover target data directory must be empty")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)

    migration = await rehearse_legacy_v1_memory_migration(
        source_copy=source,
        target_db=target / "structured-memory.sqlite",
        chat_id=chat_id,
    )

    preferences_path = target / "runtime-preferences.sqlite"
    preferences = SQLiteRuntimePreferenceStore(preferences_path)
    try:
        preferences.set_memory_enabled(chat_id, migration.memory_enabled)
        if preferences.memory_enabled(chat_id) != migration.memory_enabled:
            raise RuntimeError("cutover memory preference verification failed")
    finally:
        preferences.close()

    for database in (target / "structured-memory.sqlite", preferences_path):
        if database.exists():
            database.chmod(0o600)

    return CutoverPreparationReport(
        migration=migration,
        memory_preference_applied=True,
    )
