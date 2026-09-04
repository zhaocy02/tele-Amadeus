from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, SecretStr, field_validator

EnvironmentName = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
LLMProviderName = Literal["cpa", "deepseek", "doubao"]
WebSearchProviderName = Literal["cpa", "deepseek"]


class ConfigurationError(ValueError):
    """Raised when required application configuration is missing or invalid."""


class AppSettings(BaseModel):
    """Validated process configuration without implicit access to global environment state."""

    environment: EnvironmentName = "development"
    telegram_bot_token: SecretStr
    allowed_user_ids: frozenset[int]
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_proxy_url: str | None = None
    enable_long_polling: bool = False
    enable_autonomy_pilot: bool = False
    enable_spontaneity: bool = False
    enable_canon_examples: bool = False
    enable_web_search: bool = False
    enable_github_feedback: bool = False
    autonomy_timezone: str = "Asia/Shanghai"

    # GitHub feedback is a pinned public-repository capability, not generic GitHub access.
    github_app_client_id: str | None = None
    github_app_installation_id: int | None = None
    github_app_private_key_path: Path | None = None

    # Existing AMADEUS_PROVIDER_* settings remain the CPA/Codex profile for compatibility.
    provider_base_url: str
    provider_api_key: SecretStr | None = None
    provider_model: str = "gpt-5.6-sol"
    provider_reasoning_effort: ReasoningEffort = "high"

    llm_provider: LLMProviderName = "cpa"
    web_search_provider: WebSearchProviderName = "cpa"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: SecretStr | None = None
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_vision_model: str = "deepseek-v4-flash-vision-exp"
    deepseek_reasoning_effort: ReasoningEffort = "high"
    doubao_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    doubao_api_key: SecretStr | None = None
    doubao_model: str = "doubao-seed-evolving"
    doubao_vision_model: str = "doubao-seed-evolving"
    # Ark Responses uses its own thinking controls. Keep the generic adapter neutral by default.
    doubao_reasoning_effort: ReasoningEffort = "none"

    request_timeout_seconds: float = 120.0
    persona_dir: Path
    persona_v2_path: Path
    data_dir: Path
    memory_db_path: Path
    runtime_db_path: Path
    log_level: LogLevel = "INFO"

    @field_validator(
        "provider_base_url",
        "deepseek_base_url",
        "doubao_base_url",
        "telegram_api_base_url",
    )
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("URL must use http:// or https://")
        return normalized

    @field_validator("telegram_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if not normalized.startswith(("http://", "https://", "socks5://", "socks5h://")):
            raise ValueError("telegram_proxy_url uses an unsupported scheme")
        return normalized

    @field_validator(
        "provider_model",
        "deepseek_model",
        "deepseek_vision_model",
        "doubao_model",
        "doubao_vision_model",
    )
    @classmethod
    def validate_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider model must not be empty")
        return normalized

    @field_validator("github_app_client_id")
    @classmethod
    def validate_github_client_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("github_app_installation_id")
    @classmethod
    def validate_github_installation_id(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("github_app_installation_id must be positive")
        return value

    @field_validator("autonomy_timezone")
    @classmethod
    def validate_autonomy_timezone(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("autonomy_timezone must not be empty")
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("autonomy_timezone must be a valid IANA timezone") from exc
        return normalized

    @field_validator("allowed_user_ids")
    @classmethod
    def validate_allowed_user_ids(cls, value: frozenset[int]) -> frozenset[int]:
        if not value or any(user_id <= 0 for user_id in value):
            raise ValueError("allowed_user_ids must contain positive Telegram user IDs")
        return value

    @field_validator("request_timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if value <= 0 or value > 600:
            raise ValueError("request_timeout_seconds must be between 0 and 600")
        return value


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"missing required environment variable: {name}")
    return value


def _parse_allowed_users(raw: str) -> frozenset[int]:
    try:
        user_ids = frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ConfigurationError(
            "AMADEUS_ALLOWED_USER_IDS must be comma-separated integers"
        ) from exc
    if not user_ids:
        raise ConfigurationError("AMADEUS_ALLOWED_USER_IDS must contain at least one user ID")
    return user_ids


def _parse_environment(raw: str) -> EnvironmentName:
    normalized = raw.strip().lower() or "development"
    if normalized not in {"development", "test", "production"}:
        raise ConfigurationError(
            "AMADEUS_ENVIRONMENT must be one of: development, test, production"
        )
    return cast(EnvironmentName, normalized)


def _parse_log_level(raw: str) -> LogLevel:
    normalized = raw.strip().upper() or "INFO"
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(
            "AMADEUS_LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL"
        )
    return cast(LogLevel, normalized)


def _parse_reasoning_effort(raw: str, name: str) -> ReasoningEffort:
    normalized = raw.strip().lower() or "high"
    if normalized not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ConfigurationError(
            f"{name} must be one of: none, low, medium, high, xhigh, max"
        )
    return cast(ReasoningEffort, normalized)


def _parse_llm_provider_name(raw: str, name: str) -> LLMProviderName:
    normalized = raw.strip().casefold() or "cpa"
    aliases = {
        "codex": "cpa",
        "cpa/codex": "cpa",
        "ds": "deepseek",
        "豆包": "doubao",
        "ark": "doubao",
        "volcengine": "doubao",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"cpa", "deepseek", "doubao"}:
        raise ConfigurationError(f"{name} must be one of: cpa, deepseek, doubao")
    return cast(LLMProviderName, normalized)


def _parse_web_search_provider_name(raw: str, name: str) -> WebSearchProviderName:
    normalized = raw.strip().casefold() or "cpa"
    aliases = {"codex": "cpa", "cpa/codex": "cpa", "ds": "deepseek"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"cpa", "deepseek"}:
        raise ConfigurationError(f"{name} must be one of: cpa, deepseek")
    return cast(WebSearchProviderName, normalized)


def _parse_bool(raw: str, name: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise ConfigurationError(f"{name} must be true/false")


def _parse_optional_positive_int(raw: str, name: str) -> int | None:
    normalized = raw.strip()
    if not normalized:
        return None
    try:
        value = int(normalized)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _parse_timeout(raw: str) -> float:
    try:
        value = float(raw.strip() or "120")
    except ValueError as exc:
        raise ConfigurationError("AMADEUS_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
    if value <= 0 or value > 600:
        raise ConfigurationError("AMADEUS_REQUEST_TIMEOUT_SECONDS must be between 0 and 600")
    return value


def _resolve_path(root: Path, raw: str) -> Path:
    path = Path(raw.strip())
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def load_settings(
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> AppSettings:
    """Load settings from an explicit environment mapping.

    Importing this module never reads secrets. Global ``os.environ`` is consulted only when this
    function is called without an explicit mapping, which keeps tests and composition roots
    deterministic.
    """

    source = os.environ if env is None else env
    root = Path.cwd() if cwd is None else cwd
    data_dir_raw = source.get("AMADEUS_DATA_DIR", "./data").strip() or "./data"
    data_dir = _resolve_path(root, data_dir_raw)
    provider_api_key = source.get("AMADEUS_PROVIDER_API_KEY", "").strip()
    deepseek_api_key = source.get("AMADEUS_DEEPSEEK_API_KEY", "").strip()
    doubao_api_key = source.get("AMADEUS_DOUBAO_API_KEY", "").strip()
    telegram_proxy_url = source.get("AMADEUS_TELEGRAM_PROXY_URL", "").strip() or None
    enable_github_feedback = _parse_bool(
        source.get("AMADEUS_ENABLE_GITHUB_FEEDBACK", "false"),
        "AMADEUS_ENABLE_GITHUB_FEEDBACK",
    )
    github_app_client_id = source.get("AMADEUS_GITHUB_APP_CLIENT_ID", "").strip() or None
    github_app_installation_id = _parse_optional_positive_int(
        source.get("AMADEUS_GITHUB_APP_INSTALLATION_ID", ""),
        "AMADEUS_GITHUB_APP_INSTALLATION_ID",
    )
    github_key_raw = source.get("AMADEUS_GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
    github_app_private_key_path = _resolve_path(root, github_key_raw) if github_key_raw else None
    if enable_github_feedback:
        missing = []
        if github_app_client_id is None:
            missing.append("AMADEUS_GITHUB_APP_CLIENT_ID")
        if github_app_installation_id is None:
            missing.append("AMADEUS_GITHUB_APP_INSTALLATION_ID")
        if github_app_private_key_path is None:
            missing.append("AMADEUS_GITHUB_APP_PRIVATE_KEY_PATH")
        if missing:
            joined = ", ".join(missing)
            raise ConfigurationError(f"GitHub feedback enabled but configuration missing: {joined}")

    try:
        return AppSettings(
            environment=_parse_environment(source.get("AMADEUS_ENVIRONMENT", "development")),
            telegram_bot_token=SecretStr(_required(source, "AMADEUS_TELEGRAM_BOT_TOKEN")),
            allowed_user_ids=_parse_allowed_users(_required(source, "AMADEUS_ALLOWED_USER_IDS")),
            telegram_api_base_url=source.get(
                "AMADEUS_TELEGRAM_API_BASE_URL", "https://api.telegram.org"
            ),
            telegram_proxy_url=telegram_proxy_url,
            enable_long_polling=_parse_bool(
                source.get("AMADEUS_ENABLE_LONG_POLLING", "false"),
                "AMADEUS_ENABLE_LONG_POLLING",
            ),
            enable_autonomy_pilot=_parse_bool(
                source.get("AMADEUS_ENABLE_AUTONOMY_PILOT", "false"),
                "AMADEUS_ENABLE_AUTONOMY_PILOT",
            ),
            enable_spontaneity=_parse_bool(
                source.get("AMADEUS_ENABLE_SPONTANEITY", "false"),
                "AMADEUS_ENABLE_SPONTANEITY",
            ),
            enable_canon_examples=_parse_bool(
                source.get("AMADEUS_ENABLE_CANON_EXAMPLES", "false"),
                "AMADEUS_ENABLE_CANON_EXAMPLES",
            ),
            enable_web_search=_parse_bool(
                source.get("AMADEUS_ENABLE_WEB_SEARCH", "false"),
                "AMADEUS_ENABLE_WEB_SEARCH",
            ),
            enable_github_feedback=enable_github_feedback,
            github_app_client_id=github_app_client_id,
            github_app_installation_id=github_app_installation_id,
            github_app_private_key_path=github_app_private_key_path,
            autonomy_timezone=source.get("AMADEUS_AUTONOMY_TIMEZONE", "Asia/Shanghai"),
            provider_base_url=_required(source, "AMADEUS_PROVIDER_BASE_URL"),
            provider_api_key=SecretStr(provider_api_key) if provider_api_key else None,
            provider_model=source.get("AMADEUS_PROVIDER_MODEL", "gpt-5.6-sol"),
            provider_reasoning_effort=_parse_reasoning_effort(
                source.get("AMADEUS_PROVIDER_REASONING_EFFORT", "high"),
                "AMADEUS_PROVIDER_REASONING_EFFORT",
            ),
            llm_provider=_parse_llm_provider_name(
                source.get("AMADEUS_LLM_PROVIDER", "cpa"),
                "AMADEUS_LLM_PROVIDER",
            ),
            web_search_provider=_parse_web_search_provider_name(
                source.get("AMADEUS_WEB_SEARCH_PROVIDER", "cpa"),
                "AMADEUS_WEB_SEARCH_PROVIDER",
            ),
            deepseek_base_url=source.get(
                "AMADEUS_DEEPSEEK_BASE_URL",
                "https://api.deepseek.com",
            ),
            deepseek_api_key=SecretStr(deepseek_api_key) if deepseek_api_key else None,
            deepseek_model=source.get("AMADEUS_DEEPSEEK_MODEL", "deepseek-v4-pro"),
            deepseek_vision_model=source.get(
                "AMADEUS_DEEPSEEK_VISION_MODEL",
                "deepseek-v4-flash-vision-exp",
            ),
            deepseek_reasoning_effort=_parse_reasoning_effort(
                source.get("AMADEUS_DEEPSEEK_REASONING_EFFORT", "high"),
                "AMADEUS_DEEPSEEK_REASONING_EFFORT",
            ),
            doubao_base_url=source.get(
                "AMADEUS_DOUBAO_BASE_URL",
                "https://ark.cn-beijing.volces.com/api/v3",
            ),
            doubao_api_key=SecretStr(doubao_api_key) if doubao_api_key else None,
            doubao_model=source.get(
                "AMADEUS_DOUBAO_MODEL",
                "doubao-seed-evolving",
            ),
            doubao_vision_model=source.get(
                "AMADEUS_DOUBAO_VISION_MODEL",
                "doubao-seed-evolving",
            ),
            doubao_reasoning_effort=_parse_reasoning_effort(
                source.get("AMADEUS_DOUBAO_REASONING_EFFORT", "none"),
                "AMADEUS_DOUBAO_REASONING_EFFORT",
            ),
            request_timeout_seconds=_parse_timeout(
                source.get("AMADEUS_REQUEST_TIMEOUT_SECONDS", "120")
            ),
            persona_dir=_resolve_path(
                root, source.get("AMADEUS_PERSONA_DIR", "./profiles/legacy_v1")
            ),
            persona_v2_path=_resolve_path(
                root, source.get("AMADEUS_PERSONA_V2_PATH", "./profiles/v2/persona_core.json"),
            ),
            data_dir=data_dir,
            memory_db_path=_resolve_path(
                root, source.get("AMADEUS_MEMORY_DB", str(data_dir / "amadeus-memory.sqlite"))
            ),
            runtime_db_path=_resolve_path(
                root, source.get("AMADEUS_RUNTIME_DB", str(data_dir / "runtime.sqlite"))
            ),
            log_level=_parse_log_level(source.get("AMADEUS_LOG_LEVEL", "INFO")),
        )
    except ValueError as exc:
        if isinstance(exc, ConfigurationError):
            raise
        raise ConfigurationError(str(exc)) from exc
