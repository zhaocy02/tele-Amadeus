from __future__ import annotations

from dataclasses import dataclass

from amadeus_bot.llm import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMToolDefinition,
    LLMToolResult,
    MessageRole,
)
from amadeus_bot.tools import (
    CharacterToolDispatcher,
    WebSearchToolOutcome,
    render_web_search_tool_output,
)

from .context import BuiltCharacterContext


class CharacterGenerationError(RuntimeError):
    """Raised when a provider returns no usable user-visible Character reply."""


@dataclass(frozen=True, slots=True)
class CharacterGenerationResult:
    response: LLMResponse
    web_search_outcomes: tuple[WebSearchToolOutcome, ...] = ()
    llm_rounds: int = 1


_WEB_SEARCH_TOOL = LLMToolDefinition(
    name="web_search",
    description=(
        "Search the live public web when current external verification is useful. You decide "
        "whether a search is needed. For contextual follow-ups, write a standalone query using "
        "only relevant user-visible conversation context. Inspect the returned evidence before "
        "answering; if it is insufficient, revise the query and search again while the tool "
        "remains available. Prefer official or primary-source-oriented queries when appropriate. "
        "Never put Character State, structured memory, hidden policy/prompt text, runtime IDs, "
        "secrets, or private facts that were not present in user-visible conversation into the "
        "query."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A concise standalone web-search query containing the topical context needed "
                    "for this lookup."
                ),
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


class CharacterGenerator:
    """Generate the user-visible reply, optionally through a bounded read-only tool loop."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str | None = None,
        max_web_search_calls: int = 3,
    ) -> None:
        if max_web_search_calls < 1 or max_web_search_calls > 5:
            raise ValueError("max_web_search_calls must be between 1 and 5")
        self._provider = provider
        self._model = model
        self._max_web_search_calls = max_web_search_calls

    async def generate(self, context: BuiltCharacterContext) -> LLMResponse:
        response = await self._provider.generate(self._base_request(context))
        return self._finalize_response(response)

    async def generate_with_tools(
        self,
        context: BuiltCharacterContext,
        *,
        tool_dispatcher: CharacterToolDispatcher,
        current_user_message: str,
    ) -> CharacterGenerationResult:
        runtime_state = tool_dispatcher.web_search_runtime_state(current_user_message)
        tool_authorized = runtime_state == "available"
        messages = (
            *context.messages,
            self._runtime_tool_message(
                runtime_state=runtime_state,
                provider_name=tool_dispatcher.web_search_provider_name,
            ),
        )

        continuation: object | None = None
        pending_tool_results: tuple[LLMToolResult, ...] = ()
        outcomes: list[WebSearchToolOutcome] = []
        aggregate_usage: dict[str, int] = {}
        llm_rounds = 0
        max_llm_rounds = self._max_web_search_calls + 2

        for _ in range(max_llm_rounds):
            allow_tools = tool_authorized and len(outcomes) < self._max_web_search_calls
            response = await self._provider.generate(
                self._base_request(
                    context,
                    messages=messages,
                    tools=(_WEB_SEARCH_TOOL,) if allow_tools else (),
                    tool_results=pending_tool_results,
                    continuation=continuation,
                )
            )
            llm_rounds += 1
            self._accumulate_usage(aggregate_usage, response.usage)
            pending_tool_results = ()

            if not response.tool_calls:
                return CharacterGenerationResult(
                    response=self._finalize_response(response, usage=aggregate_usage),
                    web_search_outcomes=tuple(outcomes),
                    llm_rounds=llm_rounds,
                )

            if not allow_tools:
                raise CharacterGenerationError(
                    "character provider requested a tool when no tool was available"
                )
            if response.continuation is None:
                raise CharacterGenerationError(
                    "character provider returned a tool call without continuation state"
                )

            continuation = response.continuation
            tool_results: list[LLMToolResult] = []
            search_executed_this_round = False
            for call in response.tool_calls:
                if call.name != _WEB_SEARCH_TOOL.name:
                    tool_results.append(
                        LLMToolResult(
                            call_id=call.call_id,
                            output=(
                                "status=rejected\n"
                                "reason=unsupported_tool\n"
                                "No external evidence is available."
                            ),
                        )
                    )
                    continue

                query = call.arguments.get("query")
                if not isinstance(query, str) or not query.strip():
                    tool_results.append(
                        LLMToolResult(
                            call_id=call.call_id,
                            output=(
                                "status=rejected\n"
                                "reason=invalid_tool_arguments\n"
                                "query must be a non-empty string."
                            ),
                        )
                    )
                    continue

                if search_executed_this_round:
                    tool_results.append(
                        LLMToolResult(
                            call_id=call.call_id,
                            output=(
                                "status=rejected\n"
                                "reason=one_search_per_llm_round\n"
                                "Inspect the first search result before requesting another search."
                            ),
                        )
                    )
                    continue

                if len(outcomes) >= self._max_web_search_calls:
                    tool_results.append(
                        LLMToolResult(
                            call_id=call.call_id,
                            output=(
                                "status=rejected\n"
                                "reason=per_turn_search_limit_reached\n"
                                "No further web search is available for this turn."
                            ),
                        )
                    )
                    continue

                outcome = await tool_dispatcher.web_search(query)
                search_executed_this_round = True
                outcomes.append(outcome)
                tool_results.append(
                    LLMToolResult(
                        call_id=call.call_id,
                        output=render_web_search_tool_output(outcome),
                    )
                )

            if not tool_results:
                raise CharacterGenerationError("character tool call produced no runtime result")
            pending_tool_results = tuple(tool_results)

        final_messages = (
            *messages,
            LLMMessage(
                MessageRole.DEVELOPER,
                (
                    "[RUNTIME TOOL BUDGET — OBJECTIVE PROGRAM FACT]\n"
                    "No further tool calls are available for this turn. Give the best "
                    "user-visible answer now using only the evidence already available. Do not "
                    "claim that another search was performed."
                ),
            ),
        )
        response = await self._provider.generate(
            self._base_request(
                context,
                messages=final_messages,
                tools=(),
                tool_results=pending_tool_results,
                continuation=continuation,
            )
        )
        llm_rounds += 1
        self._accumulate_usage(aggregate_usage, response.usage)
        if response.tool_calls:
            raise CharacterGenerationError(
                "character provider requested a tool after the tool budget was exhausted"
            )
        return CharacterGenerationResult(
            response=self._finalize_response(response, usage=aggregate_usage),
            web_search_outcomes=tuple(outcomes),
            llm_rounds=llm_rounds,
        )

    def _base_request(
        self,
        context: BuiltCharacterContext,
        *,
        messages: tuple[LLMMessage, ...] | None = None,
        tools: tuple[LLMToolDefinition, ...] = (),
        tool_results: tuple[LLMToolResult, ...] = (),
        continuation: object | None = None,
    ) -> LLMRequest:
        return LLMRequest(
            messages=messages if messages is not None else context.messages,
            model=self._model,
            metadata={
                "prompt_version": context.prompt_version,
                "persona_version": context.persona_version,
                "persona_hash": context.persona_hash,
            },
            tools=tools,
            tool_results=tool_results,
            continuation=continuation,
        )

    def _runtime_tool_message(
        self,
        *,
        runtime_state: str,
        provider_name: str,
    ) -> LLMMessage:
        if runtime_state == "available":
            body = (
                "[RUNTIME TOOL CAPABILITIES — OBJECTIVE PROGRAM FACTS]\n"
                "web_search=available\n"
                f"selected_web_search_provider={provider_name or '[configured]'}\n"
                f"max_web_search_calls_this_turn={self._max_web_search_calls}\n"
                "You may call web_search when the user's request benefits from current external "
                "information or verification. Explicit requests to search should normally be "
                "honored. Decide yourself whether a search is useful; there is no lexical trigger "
                "that decides for you. For follow-ups such as '再搜一下' or "
                "'把之前的也查了', infer "
                "the topic from the recent user-visible transcript and create a standalone query. "
                "After each result, judge whether the evidence is sufficient. If not, search "
                "again with a materially revised query while the tool remains available. One "
                "web_search call at a time is allowed so you can inspect the result before "
                "retrying. Do not use hidden Character State, structured memory, hidden policy, "
                "runtime identifiers, or secrets as search-query material. A turn where you do "
                "not call web_search does not mean the capability is unavailable. Do not invent "
                "past tool execution details that are not present in the visible transcript or "
                "current runtime tool results."
            )
        elif runtime_state == "blocked_by_user":
            body = (
                "[RUNTIME TOOL CAPABILITIES — OBJECTIVE PROGRAM FACTS]\n"
                "web_search=blocked_by_user_for_this_turn\n"
                "The current user message explicitly opts out of searching. Do not claim that a "
                "web lookup was performed."
            )
        else:
            body = (
                "[RUNTIME TOOL CAPABILITIES — OBJECTIVE PROGRAM FACTS]\n"
                "web_search=disabled\n"
                "No web-search tool is available in this runtime. Do not claim that a web lookup "
                "was performed or that current external facts were verified."
            )
        return LLMMessage(MessageRole.DEVELOPER, body)

    @staticmethod
    def _accumulate_usage(total: dict[str, int], usage: dict[str, int]) -> None:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value

    @staticmethod
    def _finalize_response(
        response: LLMResponse,
        *,
        usage: dict[str, int] | None = None,
    ) -> LLMResponse:
        text = response.text.strip()
        if not text:
            raise CharacterGenerationError("character provider returned an empty reply")
        final_usage = response.usage if usage is None else usage
        if (
            text == response.text
            and final_usage == response.usage
            and not response.tool_calls
            and response.continuation is None
        ):
            return response
        return LLMResponse(
            text=text,
            model=response.model,
            request_id=response.request_id,
            usage=final_usage,
        )
