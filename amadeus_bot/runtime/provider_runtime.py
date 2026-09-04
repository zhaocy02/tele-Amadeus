from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.character import (
    AutonomyOpportunityScheduler,
    AutonomyPlanner,
    SQLiteCharacterStateStore,
)
from amadeus_bot.llm import ProviderProfile, ProviderRegistry
from amadeus_bot.memory import StructuredMemoryRepository
from amadeus_bot.tools import WebSearchProviderRegistry

from .autonomy_runtime import AutonomyRuntimeEvaluation, AutonomyRuntimePreview
from .autonomy_store import SQLiteAutonomyRuntimeStore
from .autonomy_tuning import RuntimeAutonomyTuningControl
from .safe_autonomy_runtime import AutonomyRuntimeCoordinator as _SafeAutonomyRuntimeCoordinator
from .session_store import ConversationSessionStore


@dataclass(frozen=True, slots=True)
class ProviderPreferences:
    llm_provider: str | None = None
    web_search_provider: str | None = None
    updated_at: datetime | None = None


class SQLiteProviderPreferenceStore:
    """Persistent per-chat provider choices, isolated from Character memory/state."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_preferences (
              chat_id TEXT PRIMARY KEY,
              llm_provider TEXT,
              web_search_provider TEXT,
              updated_at INTEGER NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def get(self, chat_id: int) -> ProviderPreferences:
        self._validate_chat_id(chat_id)
        row = self._db.execute(
            """
            SELECT llm_provider, web_search_provider, updated_at
            FROM provider_preferences
            WHERE chat_id = ?
            """,
            (str(chat_id),),
        ).fetchone()
        if row is None:
            return ProviderPreferences()
        return ProviderPreferences(
            llm_provider=self._optional_text(row["llm_provider"]),
            web_search_provider=self._optional_text(row["web_search_provider"]),
            updated_at=datetime.fromtimestamp(int(row["updated_at"]), tz=UTC),
        )

    def set_llm_provider(self, chat_id: int, provider: str) -> None:
        self._set(chat_id, column="llm_provider", provider=provider)

    def set_web_search_provider(self, chat_id: int, provider: str) -> None:
        self._set(chat_id, column="web_search_provider", provider=provider)

    def _set(self, chat_id: int, *, column: str, provider: str) -> None:
        self._validate_chat_id(chat_id)
        if column not in {"llm_provider", "web_search_provider"}:
            raise ValueError("unsupported provider preference field")
        normalized = provider.strip().casefold()
        if not normalized:
            raise ValueError("provider preference must not be blank")
        now = int(time.time())
        with self._db:
            self._db.execute(
                """
                INSERT OR IGNORE INTO provider_preferences(
                  chat_id, llm_provider, web_search_provider, updated_at
                ) VALUES (?, NULL, NULL, ?)
                """,
                (str(chat_id), now),
            )
            self._db.execute(
                f"""
                UPDATE provider_preferences
                SET {column} = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (normalized, now, str(chat_id)),
            )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _validate_chat_id(chat_id: int) -> None:
        if chat_id <= 0:
            raise ValueError("chat_id must be positive")


@dataclass(frozen=True, slots=True)
class ProviderRuntimeStatus:
    llm_provider: str
    llm_label: str
    text_model: str
    vision_model: str | None
    web_search_provider: str | None
    requested_llm_provider: str | None = None
    requested_web_search_provider: str | None = None

    @property
    def llm_fell_back_to_default(self) -> bool:
        return (
            self.requested_llm_provider is not None
            and self.requested_llm_provider != self.llm_provider
        )

    @property
    def web_search_fell_back_to_default(self) -> bool:
        return (
            self.requested_web_search_provider is not None
            and self.requested_web_search_provider != self.web_search_provider
        )


class RuntimeProviderControl:
    """Resolve persistent per-chat provider choices into task-local provider contexts."""

    def __init__(
        self,
        *,
        llm_registry: ProviderRegistry,
        preferences: SQLiteProviderPreferenceStore,
        web_search_registry: WebSearchProviderRegistry | None = None,
    ) -> None:
        self._llm_registry = llm_registry
        self._preferences = preferences
        self._web_search_registry = web_search_registry

    @property
    def llm_registry(self) -> ProviderRegistry:
        return self._llm_registry

    @property
    def web_search_registry(self) -> WebSearchProviderRegistry | None:
        return self._web_search_registry

    def set_llm_provider(self, chat_id: int, provider: str) -> str:
        normalized = self._normalize_alias(provider)
        if not self._llm_registry.has_provider(normalized):
            raise ValueError(f"LLM provider is not configured: {normalized}")
        self._preferences.set_llm_provider(chat_id, normalized)
        return normalized

    def set_web_search_provider(self, chat_id: int, provider: str) -> str:
        registry = self._web_search_registry
        if registry is None:
            raise ValueError("Web Search is disabled")
        normalized = self._normalize_alias(provider)
        if not registry.has_provider(normalized):
            raise ValueError(f"Web Search provider is not configured: {normalized}")
        self._preferences.set_web_search_provider(chat_id, normalized)
        return normalized

    def status(self, chat_id: int) -> ProviderRuntimeStatus:
        saved = self._preferences.get(chat_id)
        llm_provider = self._effective_llm(saved.llm_provider)
        profile = self._llm_registry.profile(llm_provider)

        web_provider: str | None = None
        if self._web_search_registry is not None:
            web_provider = self._effective_web(saved.web_search_provider)

        return ProviderRuntimeStatus(
            llm_provider=llm_provider,
            llm_label=profile.label,
            text_model=profile.text_model,
            vision_model=profile.vision_model,
            web_search_provider=web_provider,
            requested_llm_provider=saved.llm_provider,
            requested_web_search_provider=saved.web_search_provider,
        )

    @contextmanager
    def use_chat(self, chat_id: int) -> Iterator[ProviderRuntimeStatus]:
        status = self.status(chat_id)
        with ExitStack() as stack:
            stack.enter_context(self._llm_registry.use(status.llm_provider))
            if (
                self._web_search_registry is not None
                and status.web_search_provider is not None
            ):
                stack.enter_context(
                    self._web_search_registry.use(status.web_search_provider)
                )
            yield status

    def llm_profile(self, chat_id: int) -> ProviderProfile:
        return self._llm_registry.profile(self.status(chat_id).llm_provider)

    def _effective_llm(self, requested: str | None) -> str:
        if requested is not None and self._llm_registry.has_provider(requested):
            return requested
        return self._llm_registry.default_provider

    def _effective_web(self, requested: str | None) -> str:
        registry = self._web_search_registry
        assert registry is not None
        if requested is not None and registry.has_provider(requested):
            return requested
        return registry.default_provider

    @staticmethod
    def _normalize_alias(provider: str) -> str:
        normalized = provider.strip().casefold()
        aliases = {
            "codex": "cpa",
            "cpa/codex": "cpa",
            "ds": "deepseek",
        }
        normalized = aliases.get(normalized, normalized)
        if not normalized:
            raise ValueError("provider name must not be blank")
        return normalized


class ProviderAwareAutonomyRuntimeCoordinator(_SafeAutonomyRuntimeCoordinator):
    """Run model-backed autonomy planning under selected provider and per-chat tuning contexts."""

    def __init__(
        self,
        *,
        provider_control: RuntimeProviderControl,
        planner: AutonomyPlanner,
        scheduler: AutonomyOpportunityScheduler,
        memory_repository: StructuredMemoryRepository,
        state_store: SQLiteCharacterStateStore,
        sessions: ConversationSessionStore,
        autonomy_store: SQLiteAutonomyRuntimeStore,
        autonomy_tuning: RuntimeAutonomyTuningControl | None = None,
    ) -> None:
        self._provider_control = provider_control
        self._autonomy_tuning = autonomy_tuning
        super().__init__(
            planner=planner,
            scheduler=scheduler,
            memory_repository=memory_repository,
            state_store=state_store,
            sessions=sessions,
            autonomy_store=autonomy_store,
        )

    async def preview(
        self,
        chat_id: int,
        *,
        at: datetime | None = None,
        do_not_disturb: bool = False,
        user_suppressed: bool = False,
        sleep_mode: bool = False,
        sleep_started_at: datetime | None = None,
    ) -> AutonomyRuntimePreview:
        tuning = self._autonomy_tuning
        if tuning is None:
            return await super().preview(
                chat_id,
                at=at,
                do_not_disturb=do_not_disturb,
                user_suppressed=user_suppressed,
                sleep_mode=sleep_mode,
                sleep_started_at=sleep_started_at,
            )
        with tuning.use_chat(chat_id):
            return await super().preview(
                chat_id,
                at=at,
                do_not_disturb=do_not_disturb,
                user_suppressed=user_suppressed,
                sleep_mode=sleep_mode,
                sleep_started_at=sleep_started_at,
            )

    async def evaluate(
        self,
        chat_id: int,
        *,
        at: datetime | None = None,
        do_not_disturb: bool = False,
        user_suppressed: bool = False,
        sleep_mode: bool = False,
        sleep_started_at: datetime | None = None,
    ) -> AutonomyRuntimeEvaluation:
        with ExitStack() as stack:
            stack.enter_context(self._provider_control.use_chat(chat_id))
            if self._autonomy_tuning is not None:
                stack.enter_context(self._autonomy_tuning.use_chat(chat_id))
            return await super().evaluate(
                chat_id,
                at=at,
                do_not_disturb=do_not_disturb,
                user_suppressed=user_suppressed,
                sleep_mode=sleep_mode,
                sleep_started_at=sleep_started_at,
            )
