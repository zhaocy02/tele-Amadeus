from pathlib import Path

import pytest

from amadeus_bot.config import ConfigurationError, load_settings


def _env() -> dict[str, str]:
    return {
        "AMADEUS_ENVIRONMENT": "test",
        "AMADEUS_TELEGRAM_BOT_TOKEN": "test-token",
        "AMADEUS_ALLOWED_USER_IDS": "123",
        "AMADEUS_PROVIDER_BASE_URL": "http://127.0.0.1:8317/v1",
        "AMADEUS_PROVIDER_API_KEY": "cpa-key",
    }


def test_deepseek_profile_is_optional_and_cpa_remains_default(tmp_path: Path) -> None:
    settings = load_settings(_env(), cwd=tmp_path)

    assert settings.llm_provider == "cpa"
    assert settings.web_search_provider == "cpa"
    assert settings.deepseek_api_key is None
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.deepseek_vision_model == "deepseek-v4-flash-vision-exp"


def test_deepseek_profile_and_provider_aliases_are_loaded(tmp_path: Path) -> None:
    env = _env()
    env.update(
        {
            "AMADEUS_LLM_PROVIDER": "ds",
            "AMADEUS_WEB_SEARCH_PROVIDER": "deepseek",
            "AMADEUS_DEEPSEEK_API_KEY": "deepseek-secret",
            "AMADEUS_DEEPSEEK_REASONING_EFFORT": "medium",
        }
    )

    settings = load_settings(env, cwd=tmp_path)

    assert settings.llm_provider == "deepseek"
    assert settings.web_search_provider == "deepseek"
    assert settings.deepseek_api_key is not None
    assert settings.deepseek_api_key.get_secret_value() == "deepseek-secret"
    assert settings.deepseek_reasoning_effort == "medium"
    assert "deepseek-secret" not in repr(settings)


def test_invalid_provider_name_is_rejected(tmp_path: Path) -> None:
    env = _env()
    env["AMADEUS_LLM_PROVIDER"] = "unknown"

    with pytest.raises(ConfigurationError, match="AMADEUS_LLM_PROVIDER"):
        load_settings(env, cwd=tmp_path)
