"""Bounded external tool capabilities for the Amadeus runtime."""

from .dispatcher import (
    CharacterToolDispatcher,
    WebSearchStatus,
    WebSearchToolOutcome,
    WebSearchTriggerDecision,
    WebSearchTriggerPolicy,
    render_web_search_evidence,
    render_web_search_tool_output,
    web_search_context_note,
)
from .responses_web_search import ResponsesWebSearchProvider, WebSearchProviderRegistry
from .web_search import (
    CPAWebSearchProvider,
    WebSearchError,
    WebSearchEvidence,
    WebSearchProvider,
    WebSearchResult,
    normalize_web_search_query,
)

__all__ = [
    "CPAWebSearchProvider",
    "CharacterToolDispatcher",
    "ResponsesWebSearchProvider",
    "WebSearchError",
    "WebSearchEvidence",
    "WebSearchProvider",
    "WebSearchProviderRegistry",
    "WebSearchResult",
    "WebSearchStatus",
    "WebSearchToolOutcome",
    "WebSearchTriggerDecision",
    "WebSearchTriggerPolicy",
    "normalize_web_search_query",
    "render_web_search_evidence",
    "render_web_search_tool_output",
    "web_search_context_note",
]
