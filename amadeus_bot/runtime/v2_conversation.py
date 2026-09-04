from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from amadeus_bot.character import (
    CharacterRetrospective,
    CharacterState,
    CharacterTurnEngine,
    CharacterTurnInput,
    CharacterTurnResult,
    ConversationAct,
    RetrospectiveContext,
    RetrospectiveMessage,
    RetrospectiveResult,
    RetrospectiveTriggerContext,
    RetrospectiveTriggerPolicy,
    SQLiteCharacterStateStore,
)
from amadeus_bot.llm import LLMImage
from amadeus_bot.memory import (
    ArchivistApplyResult,
    ArchivistContext,
    MemoryArchivist,
    MemoryKind,
    MemoryRecord,
    MemoryRetrievalResult,
    MemoryRetriever,
    StructuredMemoryRepository,
)

from .conversation import ConversationBusyError, ConversationCancelledError
from .preferences import SQLiteRuntimePreferenceStore
from .session_store import ConversationSessionStore, StoredConversationExchange


@dataclass(frozen=True, slots=True)
class PreparedV2Turn:
    """Generated user turn that has not yet been declared delivered/persisted."""

    turn_id: str
    chat_id: int
    user_text: str
    generated_at: datetime
    state: CharacterState
    retrieval: MemoryRetrievalResult
    turn_result: CharacterTurnResult
    retrieval_ms: int = 0
    user_images_count: int = 0

    @property
    def reply_text(self) -> str:
        return self.turn_result.response.text


@dataclass(frozen=True, slots=True)
class V2FinalizeResult:
    """Post-delivery persistence/cognition result; warnings never retract delivery."""

    exchange: StoredConversationExchange | None
    archivist: ArchivistApplyResult | None
    state_version: int | None
    retrospective: RetrospectiveResult | None = None
    warnings: tuple[str, ...] = ()


class V2ConversationCoordinator:
    """Compose the Character Runtime into a delivery-aware v2 user-turn path.

    Generation and delivery are intentionally split. Callers first prepare a reply, deliver that
    exact text, then finalize the delivered turn. Noncritical post-delivery cognition fails soft.
    """

    def __init__(
        self,
        *,
        turn_engine: CharacterTurnEngine,
        retriever: MemoryRetriever,
        archivist: MemoryArchivist,
        memory_repository: StructuredMemoryRepository,
        state_store: SQLiteCharacterStateStore,
        sessions: ConversationSessionStore,
        preferences: SQLiteRuntimePreferenceStore,
        retrospective: CharacterRetrospective | None = None,
        retrospective_trigger_policy: RetrospectiveTriggerPolicy | None = None,
        history_limit_messages: int = 12,
    ) -> None:
        if history_limit_messages < 0:
            raise ValueError("history_limit_messages must not be negative")
        self._turn_engine = turn_engine
        self._retriever = retriever
        self._archivist = archivist
        self._memory_repository = memory_repository
        self._state_store = state_store
        self._sessions = sessions
        self._preferences = preferences
        self._retrospective = retrospective
        self._retrospective_trigger_policy = (
            retrospective_trigger_policy
            if retrospective_trigger_policy is not None
            else RetrospectiveTriggerPolicy()
        )
        self._history_limit_messages = history_limit_messages
        self._active: dict[int, asyncio.Task[CharacterTurnResult]] = {}

    def has_active_turn(self, chat_id: int) -> bool:
        task = self._active.get(chat_id)
        return task is not None and not task.done()

    async def prepare_user_turn(
        self,
        chat_id: int,
        user_text: str,
        *,
        at: datetime | None = None,
        requires_full_answer: bool = False,
        user_images: tuple[LLMImage, ...] = (),
    ) -> PreparedV2Turn:
        text = user_text.strip()
        if not text:
            raise ValueError("message text must not be empty")
        if self.has_active_turn(chat_id):
            raise ConversationBusyError("another v2 turn is already active for this chat")

        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        state = await self._state_store.load_state()
        if state is None:
            state = CharacterState.initial(at=timestamp)

        retrieval_started = perf_counter()
        retrieval = MemoryRetrievalResult()
        if self._preferences.memory_enabled(chat_id):
            retrieval = await self._retriever.retrieve(
                text,
                limit=7,
                at=timestamp,
                mark_recalled=False,
            )
        retrieval_ms = self._elapsed_ms(retrieval_started)
        recent_conversation = self._sessions.history(
            chat_id,
            self._history_limit_messages,
        )
        recent_acts = self._recent_acts(chat_id)
        task = asyncio.create_task(
            self._turn_engine.generate_turn(
                CharacterTurnInput(
                    current_user_message=text,
                    current_user_images=user_images,
                    recent_conversation=recent_conversation,
                    recent_acts=recent_acts,
                    requires_full_answer=requires_full_answer,
                    character_state=state.render_prompt_context(at=timestamp),
                    confirmed_facts=retrieval.confirmed_facts,
                    relationship_memories=retrieval.relationship_memories,
                    character_impressions=retrieval.character_impressions,
                    open_threads=retrieval.open_threads,
                )
            )
        )
        self._active[chat_id] = task
        try:
            turn_result = await task
        except asyncio.CancelledError:
            raise ConversationCancelledError("v2 character turn was cancelled") from None
        finally:
            if self._active.get(chat_id) is task:
                self._active.pop(chat_id, None)

        return PreparedV2Turn(
            turn_id="turn_" + uuid4().hex,
            chat_id=chat_id,
            user_text=text,
            generated_at=timestamp,
            state=state,
            retrieval=retrieval,
            turn_result=turn_result,
            retrieval_ms=retrieval_ms,
            user_images_count=len(user_images),
        )

    async def finalize_delivered_turn(
        self,
        prepared: PreparedV2Turn,
        *,
        delivered_assistant_text: str | None = None,
        at: datetime | None = None,
    ) -> V2FinalizeResult:
        assistant_text = (
            prepared.reply_text
            if delivered_assistant_text is None
            else delivered_assistant_text.strip()
        )
        if not assistant_text:
            raise ValueError("delivered assistant text must not be empty")
        timestamp = at or datetime.now(UTC)
        self._validate_aware(timestamp)
        if timestamp < prepared.generated_at:
            raise ValueError("finalize timestamp must not precede generation")

        warnings: list[str] = []
        try:
            exchange = self._sessions.append_v2_exchange(
                prepared.chat_id,
                prepared.user_text,
                assistant_text,
                turn_id=prepared.turn_id,
                policy_act=prepared.turn_result.policy.act.value,
            )
        except Exception as exc:
            return V2FinalizeResult(
                exchange=None,
                archivist=None,
                state_version=None,
                warnings=(self._warning("transcript_persistence_failed", exc),),
            )

        state_version = await self._finalize_state(prepared, timestamp, warnings)
        memory_enabled = self._preferences.memory_enabled(prepared.chat_id)

        if memory_enabled and prepared.retrieval.memory_ids:
            try:
                await self._memory_repository.touch_recalled(
                    prepared.retrieval.memory_ids,
                    at=timestamp,
                )
            except Exception as exc:
                warnings.append(self._warning("recall_metadata_failed", exc))

        archivist_result: ArchivistApplyResult | None = None
        # Phase 5.5 deliberately does not turn model visual inference into durable memory. A future
        # image-memory slice can add explicit provenance/confidence semantics; until then, photo
        # turns remain transcript-visible but skip automatic Archivist extraction.
        if memory_enabled and prepared.user_images_count == 0:
            try:
                archivist_result = await self._archivist.process(
                    context=ArchivistContext(
                        user_message_id=exchange.user_message_id,
                        assistant_message_id=exchange.assistant_message_id,
                        user_text=prepared.user_text,
                        assistant_text=assistant_text,
                        candidate_memories=tuple(
                            item.memory for item in prepared.retrieval.items
                        ),
                    ),
                    repository=self._memory_repository,
                    at=timestamp,
                )
            except Exception as exc:
                warnings.append(self._warning("archivist_post_turn_failed", exc))

        retrospective_result = await self._run_retrospective(
            prepared,
            archivist_result=archivist_result,
            at=timestamp,
            warnings=warnings,
        )
        if retrospective_result is not None and retrospective_result.changed:
            state_version = retrospective_result.state.state_version

        return V2FinalizeResult(
            exchange=exchange,
            archivist=archivist_result,
            state_version=state_version,
            retrospective=retrospective_result,
            warnings=tuple(warnings),
        )

    async def cancel(self, chat_id: int) -> bool:
        task = self._active.get(chat_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def start_new_conversation(self, chat_id: int) -> None:
        if self.has_active_turn(chat_id):
            raise ConversationBusyError("cannot rotate conversation while a v2 turn is active")
        self._sessions.rotate(chat_id)

    async def _finalize_state(
        self,
        prepared: PreparedV2Turn,
        at: datetime,
        warnings: list[str],
    ) -> int | None:
        try:
            current = await self._state_store.load_state()
            if current is None:
                await self._state_store.save_state(prepared.state)
                current = prepared.state
            elif current.state_version != prepared.state.state_version:
                warnings.append("state_changed_during_turn")
                return current.state_version

            aged = current.advance_turn(at=at)
            if aged.state_version != current.state_version:
                await self._state_store.save_state(aged)
                current = aged
            return current.state_version
        except Exception as exc:
            warnings.append(self._warning("state_post_turn_failed", exc))
            return None

    async def _run_retrospective(
        self,
        prepared: PreparedV2Turn,
        *,
        archivist_result: ArchivistApplyResult | None,
        at: datetime,
        warnings: list[str],
    ) -> RetrospectiveResult | None:
        if self._retrospective is None:
            return None
        trigger = RetrospectiveTriggerContext(
            turns_since_last=self._sessions.retrospective_turns_since_last(prepared.chat_id)
        )
        if not self._retrospective_trigger_policy.should_run(trigger):
            return None

        try:
            current_state = await self._state_store.load_state()
            if current_state is None:
                warnings.append("retrospective_state_missing")
                return None

            memories = self._retrospective_memories(prepared, archivist_result)
            messages = tuple(
                RetrospectiveMessage(
                    message_id=item.message_id,
                    role=item.role,
                    content=item.content,
                )
                for item in self._sessions.history_records(prepared.chat_id, 30)
            )
            open_thread_ids = tuple(
                memory.memory_id for memory in memories if memory.kind is MemoryKind.OPEN_THREAD
            )
            result = await self._retrospective.review(
                RetrospectiveContext(
                    current_state=current_state,
                    recent_messages=messages,
                    relevant_memories=memories,
                    recent_acts=self._sessions.recent_policy_acts(prepared.chat_id, 12),
                    available_open_thread_ids=open_thread_ids,
                ),
                at=at,
            )
            if result.changed:
                await self._state_store.save_state(result.state)
            self._sessions.mark_retrospective_run(prepared.chat_id, at=at)
            if result.reason_label == "retrospective_failure":
                warnings.append("retrospective_provider_failed")
            return result
        except Exception as exc:
            warnings.append(self._warning("retrospective_post_turn_failed", exc))
            return None

    @staticmethod
    def _retrospective_memories(
        prepared: PreparedV2Turn,
        archivist_result: ArchivistApplyResult | None,
    ) -> tuple[MemoryRecord, ...]:
        selected: list[MemoryRecord] = []
        seen: set[str] = set()
        for memory in (
            *(item.memory for item in prepared.retrieval.items),
            *((archivist_result.written_records) if archivist_result is not None else ()),
        ):
            if memory.memory_id in seen:
                continue
            seen.add(memory.memory_id)
            selected.append(memory)
            if len(selected) >= 12:
                break
        return tuple(selected)

    def _recent_acts(self, chat_id: int) -> tuple[ConversationAct, ...]:
        acts: list[ConversationAct] = []
        for value in self._sessions.recent_policy_acts(chat_id, 6):
            try:
                acts.append(ConversationAct(value))
            except ValueError:
                continue
        return tuple(acts)

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int(round((perf_counter() - started) * 1000)))

    @staticmethod
    def _warning(prefix: str, exc: Exception) -> str:
        return f"{prefix}:{type(exc).__name__}"

    @staticmethod
    def _validate_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("v2 turn timestamp must be timezone-aware")
