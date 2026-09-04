import asyncio
from datetime import UTC, datetime
from pathlib import Path

from amadeus_bot.character import (
    CHARACTER_PROMPT_VERSION,
    CharacterContextBuilder,
    CharacterGenerator,
    CharacterRetrospective,
    CharacterState,
    CharacterTurnEngine,
    ConversationPolicyPlanner,
    PlausibleMistake,
    RetrospectiveTriggerPolicy,
    SQLiteCharacterStateStore,
    load_persona_core,
)
from amadeus_bot.llm import LLMRequest, LLMResponse
from amadeus_bot.memory import (
    MemoryArchivist,
    MemoryKind,
    MemoryRecord,
    MemoryRetriever,
    MemorySourceType,
    StructuredMemoryRepository,
)
from amadeus_bot.runtime import (
    ConversationSessionStore,
    SQLiteRuntimePreferenceStore,
    V2ConversationCoordinator,
)


class RoutingProvider:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self.retrospective_text = (
            '{"decision":"UPDATE","relationship_tone":"slightly warmer",'
            '"confidence":0.7,"reason_label":"test_retrospective"}'
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        prompt_version = request.metadata.get("prompt_version")
        if prompt_version == "conversation-policy-v1":
            return LLMResponse(
                text=(
                    '{"act":"DIRECT_ANSWER","intensity":0.45,'
                    '"answer_obligation":"full","memory_callback_ids":[],'
                    '"state_bias":"","reason_label":"test"}'
                ),
                model="fake-policy",
            )
        if prompt_version == CHARACTER_PROMPT_VERSION:
            return LLMResponse(text="  这是 v2 的角色回复。  ", model="fake-character")
        if prompt_version == "memory-archivist-v1":
            return LLMResponse(
                text='{"decision":"NO_WRITE","items":[],"reason_label":"ordinary_turn"}',
                model="fake-archivist",
            )
        if prompt_version == "character-retrospective-v1":
            return LLMResponse(text=self.retrospective_text, model="fake-retrospective")
        raise AssertionError(f"unexpected prompt version: {prompt_version!r}")


class RuntimeFixture:
    def __init__(
        self,
        tmp_path: Path,
        *,
        retrospective_interval: int | None = None,
    ) -> None:
        self.provider = RoutingProvider()
        self.memory = StructuredMemoryRepository(tmp_path / "memory.sqlite")
        self.state = SQLiteCharacterStateStore(tmp_path / "state.sqlite")
        self.sessions = ConversationSessionStore(tmp_path / "runtime.sqlite")
        self.preferences = SQLiteRuntimePreferenceStore(tmp_path / "preferences.sqlite")
        persona = load_persona_core(Path("profiles/v2/persona_core.json"))
        policy = ConversationPolicyPlanner(
            provider=self.provider,
            persona=persona.core,
        )
        engine = CharacterTurnEngine(
            policy_planner=policy,
            context_builder=CharacterContextBuilder(persona),
            generator=CharacterGenerator(provider=self.provider),
        )
        retrospective = (
            CharacterRetrospective(provider=self.provider)
            if retrospective_interval is not None
            else None
        )
        trigger_policy = (
            RetrospectiveTriggerPolicy(interval_turns=retrospective_interval)
            if retrospective_interval is not None
            else None
        )
        self.coordinator = V2ConversationCoordinator(
            turn_engine=engine,
            retriever=MemoryRetriever(self.memory),
            archivist=MemoryArchivist(provider=self.provider, id_factory=lambda: "mem_new"),
            memory_repository=self.memory,
            state_store=self.state,
            sessions=self.sessions,
            preferences=self.preferences,
            retrospective=retrospective,
            retrospective_trigger_policy=trigger_policy,
        )

    def close(self) -> None:
        self.memory.close()
        self.state.close()
        self.sessions.close()
        self.preferences.close()


def _at(hour: int = 12) -> datetime:
    return datetime(2026, 9, 2, hour, 0, tzinfo=UTC)


def test_prepare_then_finalize_preserves_delivery_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path)
        try:
            prepared = await runtime.coordinator.prepare_user_turn(
                42,
                "请测试一下新的角色运行时。",
                at=_at(),
            )

            assert prepared.reply_text == "这是 v2 的角色回复。"
            assert prepared.turn_result.timing.policy_mode == "fast"
            assert runtime.sessions.message_count(42) == 0
            assert await runtime.state.load_state() is None
            prompt_versions = [
                request.metadata.get("prompt_version") for request in runtime.provider.requests
            ]
            assert prompt_versions == [CHARACTER_PROMPT_VERSION]

            finalized = await runtime.coordinator.finalize_delivered_turn(
                prepared,
                at=_at(13),
            )

            assert finalized.exchange is not None
            assert finalized.exchange.user_message_id > 0
            assert finalized.exchange.assistant_message_id > finalized.exchange.user_message_id
            assert finalized.warnings == ()
            assert finalized.state_version == 1
            assert finalized.retrospective is None
            assert runtime.sessions.message_count(42) == 2
            assert runtime.sessions.recent_policy_acts(42) == ("DIRECT_ANSWER",)
            assert await runtime.state.load_state() is not None
            assert runtime.provider.requests[-1].metadata.get("prompt_version") == (
                "memory-archivist-v1"
            )
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_retrieved_memory_reaches_character_context_and_is_marked_after_delivery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path)
        try:
            memory = MemoryRecord(
                memory_id="mem_ramen",
                kind=MemoryKind.PREFERENCE,
                content="用户明确表示喜欢拉面。",
                confidence=0.98,
                salience=0.9,
                created_at=_at(10),
                source_message_ids=(),
                source_type=MemorySourceType.MANUAL,
                tags=("拉面",),
            )
            await runtime.memory.upsert(memory)

            prepared = await runtime.coordinator.prepare_user_turn(
                42,
                "你还记得我喜欢拉面吗？",
                at=_at(),
            )

            assert prepared.retrieval.memory_ids == ("mem_ramen",)
            generation_requests = [
                request
                for request in runtime.provider.requests
                if request.metadata.get("prompt_version") == CHARACTER_PROMPT_VERSION
            ]
            assert "用户明确表示喜欢拉面" in generation_requests[0].messages[0].content
            before = await runtime.memory.get("mem_ramen")
            assert before is not None
            assert before.last_recalled_at is None

            await runtime.coordinator.finalize_delivered_turn(prepared, at=_at(13))

            after = await runtime.memory.get("mem_ramen")
            assert after is not None
            assert after.last_recalled_at == _at(13)
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_memory_off_skips_retrieval_and_archivist(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path)
        try:
            await runtime.memory.upsert(
                MemoryRecord(
                    memory_id="mem_ramen",
                    kind=MemoryKind.PREFERENCE,
                    content="用户明确表示喜欢拉面。",
                    confidence=0.98,
                    salience=0.9,
                    created_at=_at(10),
                    source_message_ids=(),
                    source_type=MemorySourceType.MANUAL,
                    tags=("拉面",),
                )
            )
            runtime.preferences.set_memory_enabled(42, False)

            prepared = await runtime.coordinator.prepare_user_turn(
                42,
                "你还记得我喜欢拉面吗？",
                at=_at(),
            )
            assert prepared.retrieval.memory_ids == ()

            finalized = await runtime.coordinator.finalize_delivered_turn(prepared, at=_at(13))
            assert finalized.exchange is not None
            assert finalized.archivist is None
            prompt_versions = [
                request.metadata.get("prompt_version") for request in runtime.provider.requests
            ]
            assert "memory-archivist-v1" not in prompt_versions
            stored = await runtime.memory.get("mem_ramen")
            assert stored is not None
            assert stored.last_recalled_at is None
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_policy_act_history_is_available_to_next_deliberate_policy_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path)
        try:
            first = await runtime.coordinator.prepare_user_turn(42, "你是不是喜欢我？", at=_at())
            await runtime.coordinator.finalize_delivered_turn(first, at=_at(13))
            second = await runtime.coordinator.prepare_user_turn(
                42,
                "你刚才为什么回避这个问题？",
                at=_at(14),
            )

            assert first.turn_result.timing.policy_mode == "llm"
            assert second.turn_result.timing.policy_mode == "llm"
            policy_requests = [
                request
                for request in runtime.provider.requests
                if request.metadata.get("prompt_version") == "conversation-policy-v1"
            ]
            assert len(policy_requests) == 2
            assert "DIRECT_ANSWER" in policy_requests[1].messages[1].content
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_duplicate_finalize_fails_soft_without_duplicate_messages(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path)
        try:
            prepared = await runtime.coordinator.prepare_user_turn(42, "一次就够了", at=_at())
            first = await runtime.coordinator.finalize_delivered_turn(prepared, at=_at(13))
            second = await runtime.coordinator.finalize_delivered_turn(prepared, at=_at(14))

            assert first.exchange is not None
            assert second.exchange is None
            assert second.warnings == ("transcript_persistence_failed:IntegrityError",)
            assert runtime.sessions.message_count(42) == 2
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_post_delivery_state_turn_aging_is_persisted(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path)
        try:
            initial = CharacterState(
                state_version=1,
                updated_at=_at(10),
                plausible_mistakes=(
                    PlausibleMistake(
                        claim="用户可能又在熬夜做项目。",
                        confidence=0.5,
                        ttl_turns=2,
                        source_message_ids=(90,),
                    ),
                ),
            )
            await runtime.state.save_state(initial)

            prepared = await runtime.coordinator.prepare_user_turn(
                42,
                "不是项目，是时差。",
                at=_at(),
            )
            finalized = await runtime.coordinator.finalize_delivered_turn(prepared, at=_at(13))
            stored = await runtime.state.load_state()

            assert finalized.state_version == 2
            assert stored is not None
            assert stored.state_version == 2
            assert stored.plausible_mistakes[0].ttl_turns == 1
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_retrospective_runs_only_after_persistent_turn_threshold(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path, retrospective_interval=2)
        try:
            first = await runtime.coordinator.prepare_user_turn(42, "第一轮", at=_at())
            first_result = await runtime.coordinator.finalize_delivered_turn(first, at=_at(13))
            assert first_result.retrospective is None
            assert runtime.sessions.retrospective_turns_since_last(42) == 1

            second = await runtime.coordinator.prepare_user_turn(42, "第二轮", at=_at(14))
            second_result = await runtime.coordinator.finalize_delivered_turn(second, at=_at(15))
            stored = await runtime.state.load_state()

            assert second_result.retrospective is not None
            assert second_result.retrospective.changed is True
            assert second_result.retrospective.reason_label == "test_retrospective"
            assert second_result.state_version == 2
            assert second_result.warnings == ()
            assert stored is not None
            assert stored.relationship_tone == "slightly warmer"
            assert runtime.sessions.retrospective_turns_since_last(42) == 0

            retrospective_requests = [
                request
                for request in runtime.provider.requests
                if request.metadata.get("prompt_version") == "character-retrospective-v1"
            ]
            assert len(retrospective_requests) == 1
        finally:
            runtime.close()

    asyncio.run(scenario())


def test_retrospective_failure_keeps_state_and_fails_soft(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = RuntimeFixture(tmp_path, retrospective_interval=2)
        runtime.provider.retrospective_text = "not-json"
        try:
            first = await runtime.coordinator.prepare_user_turn(42, "第一轮", at=_at())
            await runtime.coordinator.finalize_delivered_turn(first, at=_at(13))
            second = await runtime.coordinator.prepare_user_turn(42, "第二轮", at=_at(14))
            result = await runtime.coordinator.finalize_delivered_turn(second, at=_at(15))
            stored = await runtime.state.load_state()

            assert result.exchange is not None
            assert result.retrospective is not None
            assert result.retrospective.changed is False
            assert result.retrospective.reason_label == "retrospective_failure"
            assert result.warnings == ("retrospective_provider_failed",)
            assert result.state_version == 1
            assert stored is not None
            assert stored.state_version == 1
            assert runtime.sessions.retrospective_turns_since_last(42) == 0
        finally:
            runtime.close()

    asyncio.run(scenario())
