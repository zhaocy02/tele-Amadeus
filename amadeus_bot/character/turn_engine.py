from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Literal

from amadeus_bot.llm import LLMImage, LLMMessage, LLMResponse
from amadeus_bot.tools import CharacterToolDispatcher, WebSearchToolOutcome

from .canon import CanonRetriever, render_canon_reference
from .context import BuiltCharacterContext, CharacterContextBuilder, CharacterContextSources
from .generator import CharacterGenerator
from .policy import ConversationAct, ConversationPolicy, ConversationPolicyPlanner, PolicyContext


@dataclass(frozen=True, slots=True)
class CharacterTurnInput:
    current_user_message: str
    current_user_images: tuple[LLMImage, ...] = ()
    recent_conversation: tuple[LLMMessage, ...] = ()
    recent_acts: tuple[ConversationAct, ...] = ()
    requires_full_answer: bool = False
    character_state: str = ""
    confirmed_facts: tuple[str, ...] = ()
    relationship_memories: tuple[str, ...] = ()
    character_impressions: tuple[str, ...] = ()
    open_threads: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.current_user_message.strip():
            raise ValueError("current_user_message must not be empty")


@dataclass(frozen=True, slots=True)
class CharacterTurnTiming:
    policy_ms: int
    context_ms: int
    generation_ms: int
    total_ms: int
    policy_mode: Literal["fast", "llm"] = "llm"
    web_search_ms: int = 0
    llm_rounds: int = 1


@dataclass(frozen=True, slots=True)
class CharacterTurnResult:
    policy: ConversationPolicy
    context: BuiltCharacterContext
    response: LLMResponse
    timing: CharacterTurnTiming
    web_search_outcomes: tuple[WebSearchToolOutcome, ...] = ()


class CharacterTurnEngine:
    """Compose policy, context construction, bounded tools, and direct-turn generation.

    This engine is intentionally stateless. Transcript persistence, structured memory, Character
    State mutation, Archivist work, and Telegram delivery remain outside this boundary. Optional
    canon retrieval is read-only and local.

    When web search is available, the Character LLM decides whether it is useful and authors a
    bounded standalone query from user-visible conversational context. Runtime authorization,
    provider selection, call limits, query bounds, and execution remain program-owned. Retrieved
    evidence is ephemeral and never becomes user memory here.
    """

    def __init__(
        self,
        *,
        policy_planner: ConversationPolicyPlanner,
        context_builder: CharacterContextBuilder,
        generator: CharacterGenerator,
        canon_retriever: CanonRetriever | None = None,
        canon_limit: int = 2,
        tool_dispatcher: CharacterToolDispatcher | None = None,
    ) -> None:
        if canon_limit < 0 or canon_limit > 4:
            raise ValueError("canon_limit must be between 0 and 4")
        self._policy_planner = policy_planner
        self._context_builder = context_builder
        self._generator = generator
        self._canon_retriever = canon_retriever
        self._canon_limit = canon_limit
        self._tool_dispatcher = tool_dispatcher

    async def generate_turn(self, turn: CharacterTurnInput) -> CharacterTurnResult:
        total_started = perf_counter()
        policy_started = perf_counter()
        policy = await self._policy_planner.plan(
            PolicyContext(
                user_message=turn.current_user_message,
                recent_conversation=self._policy_recent_conversation(turn.recent_conversation),
                recent_acts=turn.recent_acts,
                message_type="user_message",
                requires_full_answer=turn.requires_full_answer,
                state_summary=turn.character_state,
                relationship_memory_summary=self._summary(turn.relationship_memories),
                open_thread_summary=self._summary(turn.open_threads),
            )
        )
        policy_ms = self._elapsed_ms(policy_started)
        policy_mode: Literal["fast", "llm"] = (
            "fast" if policy.reason_label.startswith("fast_") else "llm"
        )

        context_started = perf_counter()
        canon_examples = self._canon_examples(turn.current_user_message, policy)
        context = self._context_builder.build(
            policy=policy,
            sources=CharacterContextSources(
                current_user_message=turn.current_user_message,
                current_user_images=turn.current_user_images,
                recent_conversation=turn.recent_conversation,
                character_state=turn.character_state,
                confirmed_facts=turn.confirmed_facts,
                relationship_memories=turn.relationship_memories,
                character_impressions=turn.character_impressions,
                open_threads=turn.open_threads,
                canon_examples=canon_examples,
            ),
        )
        context_ms = self._elapsed_ms(context_started)

        generation_started = perf_counter()
        if self._tool_dispatcher is None:
            response = await self._generator.generate(context)
            web_search_outcomes: tuple[WebSearchToolOutcome, ...] = ()
            llm_rounds = 1
        else:
            generated = await self._generator.generate_with_tools(
                context,
                tool_dispatcher=self._tool_dispatcher,
                current_user_message=turn.current_user_message,
            )
            response = generated.response
            web_search_outcomes = generated.web_search_outcomes
            llm_rounds = generated.llm_rounds
        generation_ms = self._elapsed_ms(generation_started)
        web_search_ms = sum(outcome.duration_ms for outcome in web_search_outcomes)

        return CharacterTurnResult(
            policy=policy,
            context=context,
            response=response,
            timing=CharacterTurnTiming(
                policy_ms=policy_ms,
                context_ms=context_ms,
                generation_ms=generation_ms,
                total_ms=self._elapsed_ms(total_started),
                policy_mode=policy_mode,
                web_search_ms=web_search_ms,
                llm_rounds=llm_rounds,
            ),
            web_search_outcomes=web_search_outcomes,
        )

    def _canon_examples(
        self,
        user_message: str,
        policy: ConversationPolicy,
    ) -> tuple[str, ...]:
        if self._canon_retriever is None or self._canon_limit == 0:
            return ()
        result = self._canon_retriever.retrieve(
            user_message,
            act=policy.act.value,
            limit=self._canon_limit,
        )
        return tuple(
            rendered
            for item in result.items
            if (rendered := render_canon_reference(item.example))
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int(round((perf_counter() - started) * 1000)))

    @staticmethod
    def _policy_recent_conversation(messages: tuple[LLMMessage, ...]) -> tuple[str, ...]:
        return tuple(f"{message.role.value}: {message.content}" for message in messages[-6:])

    @staticmethod
    def _summary(items: tuple[str, ...]) -> str:
        return "\n".join(f"- {item.strip()}" for item in items if item.strip())
