from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Protocol

from .provider import LLMProvider, LLMRequest, LLMResponse


class ManagedLLMProvider(LLMProvider, Protocol):
    """LLM provider owned by the application composition root."""

    async def healthcheck(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """One selectable LLM upstream plus its text/vision model mapping."""

    name: str
    provider: ManagedLLMProvider
    text_model: str
    vision_model: str | None = None
    display_name: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider profile name must not be blank")
        if not self.text_model.strip():
            raise ValueError("provider text model must not be blank")
        if self.vision_model is not None and not self.vision_model.strip():
            raise ValueError("provider vision model must not be blank")

    @property
    def label(self) -> str:
        return self.display_name.strip() or self.name

    def model_for(self, request: LLMRequest) -> str:
        has_images = any(message.images for message in request.messages)
        if not has_images:
            return self.text_model
        if self.vision_model is None:
            raise ValueError(f"provider {self.name} does not support image input")
        return self.vision_model


class ProviderRegistry:
    """Task-local LLM provider selection without leaking provider names into Character code."""

    def __init__(
        self,
        profiles: Sequence[ProviderProfile],
        *,
        default_provider: str,
    ) -> None:
        mapping: dict[str, ProviderProfile] = {}
        for profile in profiles:
            name = self._normalize(profile.name)
            if name in mapping:
                raise ValueError(f"duplicate provider profile: {name}")
            mapping[name] = profile
        if not mapping:
            raise ValueError("provider registry requires at least one profile")

        default = self._normalize(default_provider)
        if default not in mapping:
            raise ValueError(f"default provider is not configured: {default}")
        self._profiles = mapping
        self._default_provider = default
        self._selected: ContextVar[str | None] = ContextVar(
            f"amadeus_llm_provider_{id(self)}",
            default=None,
        )

    @property
    def default_provider(self) -> str:
        return self._default_provider

    @property
    def available_providers(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    @property
    def selected_provider(self) -> str:
        selected = self._selected.get()
        return selected if selected is not None else self._default_provider

    def has_provider(self, name: str) -> bool:
        return self._normalize(name) in self._profiles

    def profile(self, name: str | None = None) -> ProviderProfile:
        selected = self.selected_provider if name is None else self._normalize(name)
        try:
            return self._profiles[selected]
        except KeyError:
            raise ValueError(f"provider is not configured: {selected}") from None

    @contextmanager
    def use(self, name: str) -> Iterator[None]:
        selected = self._normalize(name)
        if selected not in self._profiles:
            raise ValueError(f"provider is not configured: {selected}")
        token = self._selected.set(selected)
        try:
            yield
        finally:
            self._selected.reset(token)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        profile = self.profile()
        model = profile.model_for(request)
        routed = replace(
            request,
            model=model,
            metadata={**request.metadata, "provider_name": profile.name},
        )
        return await profile.provider.generate(routed)

    async def healthcheck(self) -> None:
        """Healthcheck the currently selected/default upstream without probing every backup."""

        await self.profile().provider.healthcheck()

    async def aclose(self) -> None:
        closed: set[int] = set()
        for profile in self._profiles.values():
            identity = id(profile.provider)
            if identity in closed:
                continue
            closed.add(identity)
            await profile.provider.aclose()

    @staticmethod
    def _normalize(name: str) -> str:
        normalized = name.strip().casefold()
        if not normalized:
            raise ValueError("provider name must not be blank")
        return normalized
