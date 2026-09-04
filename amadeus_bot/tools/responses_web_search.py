from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

import httpx

from .web_search import (
    CPAWebSearchProvider,
    WebSearchError,
    WebSearchEvidence,
    WebSearchProvider,
)


class ResponsesWebSearchProvider(CPAWebSearchProvider):
    """Provider-neutral hosted web-search adapter for Responses-compatible upstreams."""

    def __init__(
        self,
        *,
        provider_name: str,
        provider_label: str,
        base_url: str,
        api_key: str | None,
        model: str,
        reasoning_effort: str = "low",
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_name = provider_name.strip()
        normalized_label = provider_label.strip()
        if not normalized_name:
            raise ValueError("web search provider name must not be blank")
        if not normalized_label:
            raise ValueError("web search provider label must not be blank")
        self._responses_provider_name = normalized_name
        self._responses_provider_label = normalized_label
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            client=client,
            clock=clock,
        )

    @property
    def provider_name(self) -> str:
        return self._responses_provider_name

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence:
        try:
            return await super().search(query, limit=limit)
        except WebSearchError as exc:
            text = str(exc)
            text = text.replace("CPA web search", f"{self._responses_provider_label} web search")
            text = text.replace("CPA response", f"{self._responses_provider_label} response")
            raise WebSearchError(text) from None


class WebSearchProviderRegistry:
    """Independent task-local selection for Web Search capability providers."""

    def __init__(
        self,
        providers: Mapping[str, WebSearchProvider] | Sequence[tuple[str, WebSearchProvider]],
        *,
        default_provider: str,
    ) -> None:
        items = providers.items() if isinstance(providers, Mapping) else providers
        mapping: dict[str, WebSearchProvider] = {}
        for raw_name, provider in items:
            name = self._normalize(raw_name)
            if name in mapping:
                raise ValueError(f"duplicate web search provider: {name}")
            mapping[name] = provider
        if not mapping:
            raise ValueError("web search provider registry requires at least one provider")

        default = self._normalize(default_provider)
        if default not in mapping:
            raise ValueError(f"default web search provider is not configured: {default}")
        self._providers = mapping
        self._default_provider = default
        self._selected: ContextVar[str | None] = ContextVar(
            f"amadeus_web_search_provider_{id(self)}",
            default=None,
        )

    @property
    def default_provider(self) -> str:
        return self._default_provider

    @property
    def available_providers(self) -> tuple[str, ...]:
        return tuple(self._providers)

    @property
    def selected_provider(self) -> str:
        selected = self._selected.get()
        return selected if selected is not None else self._default_provider

    @property
    def provider_name(self) -> str:
        return self._provider().provider_name

    def has_provider(self, name: str) -> bool:
        return self._normalize(name) in self._providers

    @contextmanager
    def use(self, name: str) -> Iterator[None]:
        selected = self._normalize(name)
        if selected not in self._providers:
            raise ValueError(f"web search provider is not configured: {selected}")
        token = self._selected.set(selected)
        try:
            yield
        finally:
            self._selected.reset(token)

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence:
        return await self._provider().search(query, limit=limit)

    async def aclose(self) -> None:
        closed: set[int] = set()
        for provider in self._providers.values():
            identity = id(provider)
            if identity in closed:
                continue
            closed.add(identity)
            await provider.aclose()

    def _provider(self) -> WebSearchProvider:
        return self._providers[self.selected_provider]

    @staticmethod
    def _normalize(name: str) -> str:
        normalized = name.strip().casefold()
        if not normalized:
            raise ValueError("web search provider name must not be blank")
        return normalized
