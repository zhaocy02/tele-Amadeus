import asyncio
from pathlib import Path

from amadeus_bot.app import build_v2_runtime, build_v2_telegram_application
from amadeus_bot.config import AppSettings, load_settings


def _settings(tmp_path: Path, *, enable_web_search: bool = False) -> AppSettings:
    env = {
        "AMADEUS_ENVIRONMENT": "test",
        "AMADEUS_TELEGRAM_BOT_TOKEN": "test-token",
        "AMADEUS_ALLOWED_USER_IDS": "123",
        "AMADEUS_PROVIDER_BASE_URL": "http://127.0.0.1:8000",
        "AMADEUS_PROVIDER_API_KEY": "test-provider-key",
        "AMADEUS_PERSONA_V2_PATH": str(Path("profiles/v2/persona_core.json").resolve()),
        "AMADEUS_DATA_DIR": str(tmp_path / "configured-data"),
        "AMADEUS_ENABLE_WEB_SEARCH": "true" if enable_web_search else "false",
    }
    return load_settings(env, cwd=tmp_path)


def test_build_v2_runtime_uses_isolated_data_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    override = tmp_path / "ephemeral-v2"
    app = build_v2_runtime(settings, data_dir_override=override)

    try:
        assert app.data_dir == override.resolve()
        assert app.persona.core.persona_version
        assert app.web_search_provider is None
        assert (override / "runtime.sqlite").exists()
        assert (override / "structured-memory.sqlite").exists()
        assert (override / "character-state.sqlite").exists()
        assert (override / "autonomy.sqlite").exists()
        assert (override / "turn-telemetry.sqlite").exists()
        assert not (tmp_path / "configured-data" / "v2" / "runtime.sqlite").exists()
    finally:
        asyncio.run(app.aclose())


def test_build_v2_runtime_wires_web_search_only_when_process_gate_is_enabled(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enable_web_search=True)
    app = build_v2_runtime(settings, data_dir_override=tmp_path / "web-search-v2")

    try:
        assert app.web_search_provider is not None
        assert app.web_search_provider.provider_name == "cpa-native-web-search"
    finally:
        asyncio.run(app.aclose())


def test_build_v2_telegram_application_is_explicit_and_keeps_same_isolated_runtime(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    override = tmp_path / "telegram-v2"
    app = build_v2_telegram_application(settings, data_dir_override=override)

    try:
        assert app.runtime.data_dir == override.resolve()
        assert (override / "runtime.sqlite").exists()
        assert (override / "autonomy.sqlite").exists()
        assert (override / "turn-telemetry.sqlite").exists()
    finally:
        asyncio.run(app.aclose())
