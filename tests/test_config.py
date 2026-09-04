from pathlib import Path

import pytest

from amadeus_bot.config import ConfigurationError, load_settings


def valid_env() -> dict[str, str]:
    return {
        "AMADEUS_ENVIRONMENT": "test",
        "AMADEUS_TELEGRAM_BOT_TOKEN": "top-secret-test-token",
        "AMADEUS_ALLOWED_USER_IDS": "123, 456",
        "AMADEUS_PROVIDER_BASE_URL": "http://127.0.0.1:8000/",
        "AMADEUS_PROVIDER_API_KEY": "provider-secret",
        "AMADEUS_DATA_DIR": "./state",
        "AMADEUS_LOG_LEVEL": "debug",
    }


def test_load_settings_validates_and_normalizes(tmp_path: Path) -> None:
    settings = load_settings(valid_env(), cwd=tmp_path)

    assert settings.environment == "test"
    assert settings.allowed_user_ids == frozenset({123, 456})
    assert settings.provider_base_url == "http://127.0.0.1:8000"
    assert settings.data_dir == (tmp_path / "state").resolve()
    assert settings.persona_v2_path == (tmp_path / "profiles/v2/persona_core.json").resolve()
    assert settings.log_level == "DEBUG"
    assert settings.enable_long_polling is False
    assert settings.enable_canon_examples is False
    assert settings.enable_web_search is False


def test_v2_persona_path_can_be_overridden(tmp_path: Path) -> None:
    env = valid_env()
    env["AMADEUS_PERSONA_V2_PATH"] = "./custom/persona.json"

    settings = load_settings(env, cwd=tmp_path)

    assert settings.persona_v2_path == (tmp_path / "custom/persona.json").resolve()


def test_explicit_long_polling_gate_is_strictly_parsed(tmp_path: Path) -> None:
    env = valid_env()
    env["AMADEUS_ENABLE_LONG_POLLING"] = "true"
    assert load_settings(env, cwd=tmp_path).enable_long_polling is True

    env["AMADEUS_ENABLE_LONG_POLLING"] = "maybe"
    with pytest.raises(ConfigurationError, match="AMADEUS_ENABLE_LONG_POLLING"):
        load_settings(env, cwd=tmp_path)


def test_canon_examples_gate_is_default_off_and_strictly_parsed(tmp_path: Path) -> None:
    env = valid_env()
    assert load_settings(env, cwd=tmp_path).enable_canon_examples is False

    env["AMADEUS_ENABLE_CANON_EXAMPLES"] = "true"
    assert load_settings(env, cwd=tmp_path).enable_canon_examples is True

    env["AMADEUS_ENABLE_CANON_EXAMPLES"] = "sometimes"
    with pytest.raises(ConfigurationError, match="AMADEUS_ENABLE_CANON_EXAMPLES"):
        load_settings(env, cwd=tmp_path)


def test_web_search_gate_is_default_off_and_strictly_parsed(tmp_path: Path) -> None:
    env = valid_env()
    assert load_settings(env, cwd=tmp_path).enable_web_search is False

    env["AMADEUS_ENABLE_WEB_SEARCH"] = "true"
    assert load_settings(env, cwd=tmp_path).enable_web_search is True

    env["AMADEUS_ENABLE_WEB_SEARCH"] = "sometimes"
    with pytest.raises(ConfigurationError, match="AMADEUS_ENABLE_WEB_SEARCH"):
        load_settings(env, cwd=tmp_path)


def test_settings_repr_does_not_expose_secrets(tmp_path: Path) -> None:
    settings = load_settings(valid_env(), cwd=tmp_path)

    rendered = repr(settings)
    assert "top-secret-test-token" not in rendered
    assert "provider-secret" not in rendered


def test_missing_required_setting_fails_without_reading_global_env(tmp_path: Path) -> None:
    env = valid_env()
    del env["AMADEUS_TELEGRAM_BOT_TOKEN"]

    with pytest.raises(ConfigurationError, match="AMADEUS_TELEGRAM_BOT_TOKEN"):
        load_settings(env, cwd=tmp_path)


@pytest.mark.parametrize("raw", ["", "abc", "1,abc", "0"])
def test_invalid_allowed_users_are_rejected(tmp_path: Path, raw: str) -> None:
    env = valid_env()
    env["AMADEUS_ALLOWED_USER_IDS"] = raw

    with pytest.raises(ConfigurationError):
        load_settings(env, cwd=tmp_path)
