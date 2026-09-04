from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from amadeus_bot.character import (
    AutonomyGuardConfig,
    AutonomyMessageGenerator,
    AutonomyPlanner,
    AutonomyScheduleConfig,
    CharacterContextBuilder,
    CharacterGenerator,
    CharacterTurnEngine,
    ConversationPolicyPlanner,
    LoadedPersonaCore,
    RetrospectiveTriggerPolicy,
    SpontaneityComposer,
    SpontaneityConfig,
    SpontaneityOpportunityGate,
    SQLiteCharacterStateStore,
    load_legacy_persona,
    load_persona_core,
)
from amadeus_bot.character.canon import CanonRetriever, load_canon_examples
from amadeus_bot.character.runtime_retrospective import SchemaGuidedCharacterRetrospective
from amadeus_bot.config import AppSettings, ConfigurationError
from amadeus_bot.llm import ProviderProfile, ProviderRegistry, ResponsesAPIProvider
from amadeus_bot.memory import (
    LegacyMemoryStore,
    MemoryArchivist,
    MemoryRetriever,
    StructuredMemoryRepository,
)
from amadeus_bot.runtime import (
    ConversationSessionStore,
    LegacyConversationService,
    ProviderAwareAutonomyRuntimeCoordinator,
    RuntimeAutonomyTuningControl,
    RuntimeProviderControl,
    SQLiteAutonomyRuntimeStore,
    SQLiteAutonomyTuningStore,
    SQLiteProviderPreferenceStore,
    SQLiteRuntimePreferenceStore,
    SQLiteSpontaneityStore,
    SQLiteTurnTelemetryStore,
    TunableAutonomyGuard,
    TunableAutonomyOpportunityScheduler,
    V2ConversationCoordinator,
)
from amadeus_bot.telegram import (
    GitHubFeedbackCommandRouter,
    ProviderAwareV2TelegramDeliveryAdapter,
    TelegramHTTPGateway,
    TelegramMessageRouter,
    V2TelegramDeliveryAdapter,
    V2TelegramMessageRouter,
)
from amadeus_bot.tools import (
    CharacterToolDispatcher,
    CPAWebSearchProvider,
    GitHubAppInstallationConfig,
    GitHubAppInstallationTokenProvider,
    GitHubFeedbackClient,
    ResponsesWebSearchProvider,
    WebSearchProvider,
    WebSearchProviderRegistry,
)

_GITHUB_FEEDBACK_REPOSITORY = "zhaocy02/tele-Amadeus"
_GITHUB_FEEDBACK_BRANCH = "main"
_GITHUB_FEEDBACK_LABELS = frozenset(
    {"from-amadeus", "observation", "idea", "possible-bug", "character", "memory"}
)


@dataclass(slots=True)
class AmadeusApplication:
    settings: AppSettings
    provider: ResponsesAPIProvider
    telegram: TelegramHTTPGateway
    memory: LegacyMemoryStore
    sessions: ConversationSessionStore
    conversation: LegacyConversationService
    router: TelegramMessageRouter

    async def aclose(self) -> None:
        self.memory.close()
        self.sessions.close()
        await self.provider.aclose()
        await self.telegram.aclose()


@dataclass(slots=True)
class V2RuntimeApplication:
    """Non-Telegram v2 composition root used for Server Dev integration and later cutover."""

    settings: AppSettings
    persona: LoadedPersonaCore
    provider: ProviderRegistry
    provider_preferences: SQLiteProviderPreferenceStore
    provider_control: RuntimeProviderControl
    memory: StructuredMemoryRepository
    state: SQLiteCharacterStateStore
    sessions: ConversationSessionStore
    preferences: SQLiteRuntimePreferenceStore
    autonomy_tuning: RuntimeAutonomyTuningControl
    autonomy_store: SQLiteAutonomyRuntimeStore
    spontaneity_store: SQLiteSpontaneityStore
    telemetry: SQLiteTurnTelemetryStore
    conversation: V2ConversationCoordinator
    autonomy: ProviderAwareAutonomyRuntimeCoordinator
    data_dir: Path
    web_search_provider: WebSearchProviderRegistry | None = None

    async def aclose(self) -> None:
        self.memory.close()
        self.state.close()
        self.sessions.close()
        self.preferences.close()
        self.autonomy_tuning.close()
        self.provider_preferences.close()
        self.autonomy_store.close()
        self.spontaneity_store.close()
        self.telemetry.close()
        if self.web_search_provider is not None:
            await self.web_search_provider.aclose()
        await self.provider.aclose()


@dataclass(slots=True)
class V2TelegramApplication:
    """Explicit v2 Telegram composition; polling remains an explicit caller decision."""

    runtime: V2RuntimeApplication
    telegram: TelegramHTTPGateway
    delivery: V2TelegramDeliveryAdapter
    router: V2TelegramMessageRouter
    github_feedback_client: GitHubFeedbackClient | None = None
    github_token_provider: GitHubAppInstallationTokenProvider | None = None

    async def aclose(self) -> None:
        await self.router.aclose()
        if self.github_feedback_client is not None:
            await self.github_feedback_client.aclose()
        if self.github_token_provider is not None:
            await self.github_token_provider.aclose()
        await self.telegram.aclose()
        await self.runtime.aclose()


def build_provider(settings: AppSettings) -> ResponsesAPIProvider:
    if settings.provider_api_key is None:
        raise ConfigurationError("AMADEUS_PROVIDER_API_KEY is required for the CPA parity adapter")
    return ResponsesAPIProvider(
        base_url=settings.provider_base_url,
        api_key=settings.provider_api_key.get_secret_value(),
        default_model=settings.provider_model,
        reasoning_effort=settings.provider_reasoning_effort,
        timeout_seconds=settings.request_timeout_seconds,
    )


def build_v2_provider_registry(settings: AppSettings) -> ProviderRegistry:
    profiles = [
        ProviderProfile(
            name="cpa",
            display_name="CPA/Codex",
            provider=build_provider(settings),
            text_model=settings.provider_model,
            vision_model=settings.provider_model,
        )
    ]
    if settings.deepseek_api_key is not None:
        deepseek = ResponsesAPIProvider(
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key.get_secret_value(),
            default_model=settings.deepseek_model,
            reasoning_effort=settings.deepseek_reasoning_effort,
            timeout_seconds=settings.request_timeout_seconds,
        )
        profiles.append(
            ProviderProfile(
                name="deepseek",
                display_name="DeepSeek",
                provider=deepseek,
                text_model=settings.deepseek_model,
                vision_model=settings.deepseek_vision_model,
            )
        )
    if settings.doubao_api_key is not None:
        doubao = ResponsesAPIProvider(
            base_url=settings.doubao_base_url,
            api_key=settings.doubao_api_key.get_secret_value(),
            default_model=settings.doubao_model,
            reasoning_effort=settings.doubao_reasoning_effort,
            timeout_seconds=settings.request_timeout_seconds,
        )
        profiles.append(
            ProviderProfile(
                name="doubao",
                display_name="Doubao / Volcengine Ark",
                provider=doubao,
                text_model=settings.doubao_model,
                vision_model=settings.doubao_vision_model,
            )
        )
    try:
        return ProviderRegistry(profiles, default_provider=settings.llm_provider)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def build_web_search_registry(settings: AppSettings) -> WebSearchProviderRegistry | None:
    if not settings.enable_web_search:
        return None

    provider_key = (
        settings.provider_api_key.get_secret_value()
        if settings.provider_api_key is not None
        else None
    )
    search_timezone = ZoneInfo(settings.autonomy_timezone)

    def search_clock() -> datetime:
        return datetime.now(search_timezone)

    providers: list[tuple[str, WebSearchProvider]] = [
        (
            "cpa",
            CPAWebSearchProvider(
                base_url=settings.provider_base_url,
                api_key=provider_key,
                model=settings.provider_model,
                reasoning_effort="low",
                timeout_seconds=min(settings.request_timeout_seconds, 60.0),
                clock=search_clock,
            ),
        )
    ]
    if settings.deepseek_api_key is not None:
        providers.append(
            (
                "deepseek",
                ResponsesWebSearchProvider(
                    provider_name="deepseek-native-web-search",
                    provider_label="DeepSeek",
                    base_url=settings.deepseek_base_url,
                    api_key=settings.deepseek_api_key.get_secret_value(),
                    model=settings.deepseek_model,
                    reasoning_effort="low",
                    timeout_seconds=min(settings.request_timeout_seconds, 60.0),
                    clock=search_clock,
                ),
            )
        )
    try:
        return WebSearchProviderRegistry(
            providers,
            default_provider=settings.web_search_provider,
        )
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


def build_telegram_gateway(settings: AppSettings) -> TelegramHTTPGateway:
    return TelegramHTTPGateway(
        bot_token=settings.telegram_bot_token.get_secret_value(),
        api_base_url=settings.telegram_api_base_url,
        proxy_url=settings.telegram_proxy_url,
        timeout_seconds=settings.request_timeout_seconds,
    )


def build_github_feedback_control(
    settings: AppSettings,
    *,
    telegram: TelegramHTTPGateway,
) -> tuple[
    GitHubFeedbackCommandRouter | None,
    GitHubFeedbackClient | None,
    GitHubAppInstallationTokenProvider | None,
]:
    """Compose the pinned public-Issues capability only behind the explicit process gate."""

    if not settings.enable_github_feedback:
        return None, None, None

    client_id = settings.github_app_client_id
    installation_id = settings.github_app_installation_id
    private_key_path = settings.github_app_private_key_path
    if client_id is None or installation_id is None or private_key_path is None:
        raise ConfigurationError(
            "GitHub feedback enabled without complete GitHub App configuration"
        )

    token_provider = GitHubAppInstallationTokenProvider(
        GitHubAppInstallationConfig(
            client_id=client_id,
            installation_id=installation_id,
            private_key_path=private_key_path,
            repository=_GITHUB_FEEDBACK_REPOSITORY,
            timeout_seconds=min(settings.request_timeout_seconds, 120.0),
        )
    )
    client = GitHubFeedbackClient(
        repository=_GITHUB_FEEDBACK_REPOSITORY,
        branch=_GITHUB_FEEDBACK_BRANCH,
        token_provider=token_provider,
        allowed_labels=_GITHUB_FEEDBACK_LABELS,
        timeout_seconds=min(settings.request_timeout_seconds, 120.0),
    )
    command = GitHubFeedbackCommandRouter(gateway=telegram, feedback=client)
    return command, client, token_provider


def build_application(settings: AppSettings) -> AmadeusApplication:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    persona = load_legacy_persona(settings.persona_dir)
    provider = build_provider(settings)
    telegram = build_telegram_gateway(settings)
    memory = LegacyMemoryStore(settings.memory_db_path)
    sessions = ConversationSessionStore(settings.runtime_db_path)
    conversation = LegacyConversationService(
        provider=provider,
        persona=persona,
        memory=memory,
        sessions=sessions,
        model=settings.provider_model,
    )
    router = TelegramMessageRouter(telegram, conversation)
    return AmadeusApplication(
        settings=settings,
        provider=provider,
        telegram=telegram,
        memory=memory,
        sessions=sessions,
        conversation=conversation,
        router=router,
    )


def build_v2_runtime(
    settings: AppSettings,
    *,
    data_dir_override: Path | None = None,
    retrospective_interval_turns: int = 16,
) -> V2RuntimeApplication:
    """Build the v2 cognitive runtime without binding it to Telegram delivery."""

    data_dir = (
        data_dir_override.resolve()
        if data_dir_override is not None
        else (settings.data_dir / "v2").resolve()
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    persona = load_persona_core(settings.persona_v2_path)
    provider = build_v2_provider_registry(settings)
    memory = StructuredMemoryRepository(data_dir / "structured-memory.sqlite")
    state = SQLiteCharacterStateStore(data_dir / "character-state.sqlite")
    sessions = ConversationSessionStore(data_dir / "runtime.sqlite")
    preferences_path = data_dir / "runtime-preferences.sqlite"
    preferences = SQLiteRuntimePreferenceStore(preferences_path)
    autonomy_tuning = RuntimeAutonomyTuningControl(SQLiteAutonomyTuningStore(preferences_path))
    provider_preferences = SQLiteProviderPreferenceStore(data_dir / "provider-preferences.sqlite")
    autonomy_store = SQLiteAutonomyRuntimeStore(data_dir / "autonomy.sqlite")
    spontaneity_store = SQLiteSpontaneityStore(data_dir / "spontaneity.sqlite")
    telemetry = SQLiteTurnTelemetryStore(data_dir / "turn-telemetry.sqlite")

    canon_retriever: CanonRetriever | None = None
    if settings.enable_canon_examples:
        canon_path = data_dir / "canon.jsonl"
        if canon_path.is_file():
            canon_retriever = CanonRetriever(load_canon_examples(canon_path))

    web_search_provider = build_web_search_registry(settings)
    provider_control = RuntimeProviderControl(
        llm_registry=provider,
        preferences=provider_preferences,
        web_search_registry=web_search_provider,
    )
    tool_dispatcher = CharacterToolDispatcher(web_search_provider=web_search_provider)

    policy = ConversationPolicyPlanner(
        provider=provider,
        persona=persona.core,
        model=settings.provider_model,
    )
    context_builder = CharacterContextBuilder(persona)
    generator = CharacterGenerator(provider=provider, model=settings.provider_model)
    turn_engine = CharacterTurnEngine(
        policy_planner=policy,
        context_builder=context_builder,
        generator=generator,
        canon_retriever=canon_retriever,
        tool_dispatcher=tool_dispatcher,
    )
    retriever = MemoryRetriever(memory)
    archivist = MemoryArchivist(provider=provider, model=settings.provider_model)
    retrospective = SchemaGuidedCharacterRetrospective(
        provider=provider,
        model=settings.provider_model,
    )
    conversation = V2ConversationCoordinator(
        turn_engine=turn_engine,
        retriever=retriever,
        archivist=archivist,
        memory_repository=memory,
        state_store=state,
        sessions=sessions,
        preferences=preferences,
        retrospective=retrospective,
        retrospective_trigger_policy=RetrospectiveTriggerPolicy(
            interval_turns=retrospective_interval_turns
        ),
    )
    autonomy_guard = TunableAutonomyGuard(
        AutonomyGuardConfig(
            min_user_idle=timedelta(minutes=3),
            proactive_cooldown=timedelta(minutes=30),
            max_messages_per_24h=24,
            max_consecutive_unanswered=3,
            max_messages_per_sleep_session=2,
            min_signal_salience=0.50,
            min_model_motivation=0.50,
            idle_drive_max_motivation_bonus=0.25,
        ),
        autonomy_tuning,
    )
    autonomy = ProviderAwareAutonomyRuntimeCoordinator(
        provider_control=provider_control,
        planner=AutonomyPlanner(
            provider=provider,
            guard=autonomy_guard,
            model=settings.provider_model,
        ),
        scheduler=TunableAutonomyOpportunityScheduler(
            AutonomyScheduleConfig(check_interval=timedelta(minutes=5)),
            autonomy_tuning,
        ),
        memory_repository=memory,
        state_store=state,
        sessions=sessions,
        autonomy_store=autonomy_store,
        autonomy_tuning=autonomy_tuning,
    )
    return V2RuntimeApplication(
        settings=settings,
        persona=persona,
        provider=provider,
        provider_preferences=provider_preferences,
        provider_control=provider_control,
        memory=memory,
        state=state,
        sessions=sessions,
        preferences=preferences,
        autonomy_tuning=autonomy_tuning,
        autonomy_store=autonomy_store,
        spontaneity_store=spontaneity_store,
        telemetry=telemetry,
        conversation=conversation,
        autonomy=autonomy,
        data_dir=data_dir,
        web_search_provider=web_search_provider,
    )


def build_v2_telegram_application(
    settings: AppSettings,
    *,
    data_dir_override: Path | None = None,
    retrospective_interval_turns: int = 16,
) -> V2TelegramApplication:
    """Build v2 Telegram delivery and commands without implicitly starting long polling."""

    runtime = build_v2_runtime(
        settings,
        data_dir_override=data_dir_override,
        retrospective_interval_turns=retrospective_interval_turns,
    )
    telegram = build_telegram_gateway(settings)
    github_feedback, github_feedback_client, github_token_provider = build_github_feedback_control(
        settings,
        telegram=telegram,
    )
    autonomy_generator = AutonomyMessageGenerator(
        provider=runtime.provider,
        persona=runtime.persona,
        model=settings.provider_model,
    )
    spontaneity_config = SpontaneityConfig()
    spontaneity_composer = SpontaneityComposer(
        provider=runtime.provider,
        persona=runtime.persona,
        config=spontaneity_config,
        model=settings.provider_model,
    )
    delivery = ProviderAwareV2TelegramDeliveryAdapter(
        provider_control=runtime.provider_control,
        gateway=telegram,
        conversation=runtime.conversation,
        autonomy=runtime.autonomy,
        autonomy_message_generator=autonomy_generator,
        sessions=runtime.sessions,
        preferences=runtime.preferences,
        telemetry=runtime.telemetry,
        spontaneity_store=runtime.spontaneity_store,
    )
    router = V2TelegramMessageRouter(
        provider_control=runtime.provider_control,
        github_feedback=github_feedback,
        autonomy_tuning=runtime.autonomy_tuning,
        gateway=telegram,
        delivery=delivery,
        conversation=runtime.conversation,
        memory=runtime.memory,
        state=runtime.state,
        sessions=runtime.sessions,
        preferences=runtime.preferences,
        autonomy=runtime.autonomy,
        autonomy_store=runtime.autonomy_store,
        autonomy_pilot_enabled=settings.enable_autonomy_pilot,
        autonomy_timezone=settings.autonomy_timezone,
        spontaneity_composer=spontaneity_composer,
        spontaneity_gate=SpontaneityOpportunityGate(spontaneity_config),
        spontaneity_store=runtime.spontaneity_store,
        spontaneity_process_enabled=settings.enable_spontaneity,
    )
    return V2TelegramApplication(
        runtime=runtime,
        telegram=telegram,
        delivery=delivery,
        router=router,
        github_feedback_client=github_feedback_client,
        github_token_provider=github_token_provider,
    )
