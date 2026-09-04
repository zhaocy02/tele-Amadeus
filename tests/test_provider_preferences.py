from pathlib import Path

from amadeus_bot.llm import LLMRequest, LLMResponse, ProviderProfile, ProviderRegistry
from amadeus_bot.runtime import RuntimeProviderControl, SQLiteProviderPreferenceStore


class FakeProvider:
    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text="ok", model=request.model or "fake")

    async def aclose(self) -> None:
        return None


def _registry(*names: str) -> ProviderRegistry:
    return ProviderRegistry(
        tuple(
            ProviderProfile(
                name=name,
                provider=FakeProvider(),
                text_model=f"{name}-text",
                vision_model=f"{name}-vision",
            )
            for name in names
        ),
        default_provider=names[0],
    )


def test_provider_preference_store_persists_per_chat(tmp_path: Path) -> None:
    path = tmp_path / "providers.sqlite"
    store = SQLiteProviderPreferenceStore(path)
    store.set_llm_provider(10, "deepseek")
    store.set_web_search_provider(10, "cpa")
    store.close()

    reopened = SQLiteProviderPreferenceStore(path)
    try:
        saved = reopened.get(10)
        assert saved.llm_provider == "deepseek"
        assert saved.web_search_provider == "cpa"
        assert reopened.get(11).llm_provider is None
    finally:
        reopened.close()


def test_provider_control_keeps_default_when_saved_provider_is_unavailable(tmp_path: Path) -> None:
    store = SQLiteProviderPreferenceStore(tmp_path / "providers.sqlite")
    store.set_llm_provider(10, "deepseek")
    registry = _registry("cpa")
    control = RuntimeProviderControl(
        llm_registry=registry,
        preferences=store,
    )
    try:
        status = control.status(10)
        assert status.llm_provider == "cpa"
        assert status.requested_llm_provider == "deepseek"
        assert status.llm_fell_back_to_default is True
    finally:
        store.close()
