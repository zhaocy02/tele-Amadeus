from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

_TAG_RE = re.compile(r"<[^>]+>")


class WebSearchError(RuntimeError):
    """Sanitized search-provider failure safe for runtime fail-soft handling."""


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    title: str
    url: str
    snippet: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.url.strip():
            raise ValueError("web search result title/url must not be blank")


@dataclass(frozen=True, slots=True)
class WebSearchEvidence:
    query: str
    retrieved_at: datetime
    results: tuple[WebSearchResult, ...]
    provider: str
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.query.strip() or not self.provider.strip():
            raise ValueError("web search evidence query/provider must not be blank")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("web search retrieved_at must be timezone-aware")


class WebSearchProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence: ...

    async def aclose(self) -> None: ...


def normalize_web_search_query(value: str) -> str:
    """Normalize one user-supplied query to a conservative provider-safe bound."""

    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise ValueError("web search query must not be empty")
    words = normalized.split()
    if len(words) > 50:
        normalized = " ".join(words[:50])
    normalized = normalized[:400].rstrip()
    if not normalized:
        raise ValueError("web search query must not be empty")
    return normalized


class CPAWebSearchProvider:
    """Use the existing CPA Responses path to execute server-side hosted web search.

    The provider receives only the explicit query string. It does not receive Character State,
    memory, transcript, Telegram identifiers, or hidden policy context. The upstream model may use
    the hosted ``web_search`` tool, while the Amadeus server itself only talks to its existing CPA
    endpoint.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        reasoning_effort: str = "low",
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_base = base_url.strip().rstrip("/")
        normalized_model = model.strip()
        if not normalized_base.startswith(("http://", "https://")):
            raise ValueError("CPA web search base URL must use http:// or https://")
        if not normalized_model:
            raise ValueError("CPA web search model must not be empty")
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("CPA web search timeout must be between 0 and 600 seconds")
        self._base_url = normalized_base
        self._api_key = api_key.strip() if api_key else None
        self._model = normalized_model
        self._reasoning_effort = reasoning_effort.strip().lower() or "low"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, trust_env=False)
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def provider_name(self) -> str:
        return "cpa-native-web-search"

    async def search(self, query: str, *, limit: int = 5) -> WebSearchEvidence:
        normalized = normalize_web_search_query(query)
        if limit < 1 or limit > 10:
            raise ValueError("web search limit must be between 1 and 10")

        local_now = self._clock()
        if local_now.tzinfo is None or local_now.utcoffset() is None:
            raise WebSearchError("CPA web search clock returned a naive timestamp")

        payload: dict[str, Any] = {
            "model": self._model,
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Search the live public web for this query. Use the web_search tool. "
                        "Return a concise factual synthesis grounded in the retrieved sources. "
                        f"The user's current local date/time is {local_now.isoformat()}; interpret "
                        "relative date words such as today, yesterday, 今天, 昨天, and 昨晚 "
                        "against that local date/time.\n\n"
                        f"Query: {normalized}"
                    ),
                }
            ],
            "tools": [{"type": "web_search"}],
            "tool_choice": "required",
            "store": False,
        }
        if self._reasoning_effort != "none":
            payload["reasoning"] = {"effort": self._reasoning_effort}

        try:
            response = await self._client.post(
                self._endpoint("responses"),
                headers=self._headers(),
                json=payload,
            )
        except httpx.TimeoutException:
            raise WebSearchError("CPA web search request timed out") from None
        except httpx.HTTPError:
            raise WebSearchError("CPA web search request could not be completed") from None

        if response.status_code >= 400:
            raise WebSearchError(f"CPA web search returned HTTP {response.status_code}")

        try:
            raw: object = response.json()
        except ValueError:
            raise WebSearchError("CPA web search returned invalid JSON") from None
        if not isinstance(raw, dict):
            raise WebSearchError("CPA web search returned an invalid response object")

        evidence = self._parse_response(raw, query=normalized, limit=limit)
        if not evidence.results:
            raise WebSearchError("CPA web search completed without source evidence")
        return evidence

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _parse_response(
        self,
        data: Mapping[str, object],
        *,
        query: str,
        limit: int,
    ) -> WebSearchEvidence:
        output = data.get("output")
        if not isinstance(output, list):
            raise WebSearchError("CPA web search response does not contain output")

        saw_search_call = False
        results: list[WebSearchResult] = []
        summary_parts: list[str] = []
        seen_urls: set[str] = set()

        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call":
                saw_search_call = True
                self._collect_search_call_results(
                    item,
                    results=results,
                    seen_urls=seen_urls,
                    limit=limit,
                )
                continue
            if item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    summary_parts.append(self._clean_text(text, limit=2400))
                self._collect_citations(
                    part.get("annotations"),
                    results=results,
                    seen_urls=seen_urls,
                    limit=limit,
                )

        if not saw_search_call:
            raise WebSearchError("CPA response did not execute the web_search tool")

        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise WebSearchError("CPA web search clock returned a naive timestamp")
        return WebSearchEvidence(
            query=query,
            retrieved_at=retrieved_at,
            results=tuple(results[:limit]),
            provider=self.provider_name,
            summary="\n".join(part for part in summary_parts if part).strip(),
        )

    def _collect_search_call_results(
        self,
        item: Mapping[str, object],
        *,
        results: list[WebSearchResult],
        seen_urls: set[str],
        limit: int,
    ) -> None:
        candidates: list[object] = []
        raw_results = item.get("results")
        if isinstance(raw_results, list):
            candidates.extend(raw_results)
        action = item.get("action")
        if isinstance(action, dict):
            sources = action.get("sources")
            if isinstance(sources, list):
                candidates.extend(sources)
            action_url = action.get("url")
            if isinstance(action_url, str):
                candidates.append({"url": action_url})

        for candidate in candidates:
            if len(results) >= limit:
                break
            if not isinstance(candidate, dict):
                continue
            self._append_result(candidate, results=results, seen_urls=seen_urls)

    def _collect_citations(
        self,
        annotations: object,
        *,
        results: list[WebSearchResult],
        seen_urls: set[str],
        limit: int,
    ) -> None:
        if not isinstance(annotations, list):
            return
        for annotation in annotations:
            if len(results) >= limit:
                break
            if not isinstance(annotation, dict):
                continue
            if annotation.get("type") != "url_citation":
                continue
            self._append_result(annotation, results=results, seen_urls=seen_urls)

    def _append_result(
        self,
        candidate: Mapping[str, object],
        *,
        results: list[WebSearchResult],
        seen_urls: set[str],
    ) -> None:
        url = candidate.get("url")
        if not isinstance(url, str):
            return
        clean_url = " ".join(url.split()).strip()[:1000]
        if not clean_url or clean_url in seen_urls:
            return
        title = candidate.get("title")
        title_text = self._clean_text(title, limit=220) if isinstance(title, str) else clean_url
        snippet_value = candidate.get("snippet")
        if not isinstance(snippet_value, str):
            snippet_value = candidate.get("description")
        if not isinstance(snippet_value, str):
            snippet_value = candidate.get("text")
        snippet = (
            self._clean_text(snippet_value, limit=700)
            if isinstance(snippet_value, str)
            else ""
        )
        results.append(
            WebSearchResult(
                title=title_text or clean_url,
                url=clean_url,
                snippet=snippet,
            )
        )
        seen_urls.add(clean_url)

    def _endpoint(self, resource: str) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/{resource}"
        return f"{self._base_url}/v1/{resource}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _clean_text(value: str, *, limit: int) -> str:
        without_tags = _TAG_RE.sub(" ", html.unescape(value))
        return " ".join(without_tags.split()).strip()[:limit].rstrip()
