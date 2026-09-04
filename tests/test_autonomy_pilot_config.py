from pathlib import Path

import pytest

from amadeus_bot.config import ConfigurationError, load_settings


def _env() -> dict[str, str]:
    return {
        "AMADEUS_ENVIRONMENT": "test",
        "AMADEUS_TELEGRAM_BOT_TOKEN": "telegram-test-token",
        "AMADEUS_ALLOWED_USER_IDS": "123",
        "AMADEUS_PROVIDER_BASE_URL": "http://127.0.0.1:8000",
        "AMADEUS_PROVIDER_API_KEY": "provider-test-key",
    }


def test_autonomy_and_spontaneity_process_gates_are_default_off(tmp_path: Path) -> None:
    settings = load_settings(_env(), cwd=tmp_path)

    assert settings.enable_autonomy_pilot is False
    assert settings.enable_spontaneity is False
    assert settings.autonomy_timezone == "Asia/Shanghai"


def test_autonomy_spontaneity_gates_and_timezone_are_explicit(tmp_path: Path) -> None:
    env = _env()
    env["AMADEUS_ENABLE_AUTONOMY_PILOT"] = "true"
    env["AMADEUS_ENABLE_SPONTANEITY"] = "true"
    env["AMADEUS_AUTONOMY_TIMEZONE"] = "Asia/Tokyo"

    settings = load_settings(env, cwd=tmp_path)

    assert settings.enable_autonomy_pilot is True
    assert settings.enable_spontaneity is True
    assert settings.autonomy_timezone == "Asia/Tokyo"


def test_invalid_autonomy_and_spontaneity_config_fails_closed(tmp_path: Path) -> None:
    env = _env()
    env["AMADEUS_ENABLE_AUTONOMY_PILOT"] = "maybe"
    with pytest.raises(ConfigurationError, match="AMADEUS_ENABLE_AUTONOMY_PILOT"):
        load_settings(env, cwd=tmp_path)

    env = _env()
    env["AMADEUS_ENABLE_SPONTANEITY"] = "maybe"
    with pytest.raises(ConfigurationError, match="AMADEUS_ENABLE_SPONTANEITY"):
        load_settings(env, cwd=tmp_path)

    env = _env()
    env["AMADEUS_AUTONOMY_TIMEZONE"] = "Mars/Olympus"
    with pytest.raises(ConfigurationError, match="autonomy_timezone"):
        load_settings(env, cwd=tmp_path)
