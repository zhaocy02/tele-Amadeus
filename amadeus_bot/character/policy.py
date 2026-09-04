from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from amadeus_bot.llm import LLMMessage, LLMProvider, LLMRequest, MessageRole

from .persona import PersonaCore

POLICY_PROMPT_VERSION = "conversation-policy-v1"

_SIMPLE_SOCIAL_MESSAGES = frozenset(
    {
        "早",
        "早上好",
        "早安",
        "晚安",
        "你好",
        "嗨",
        "hi",
        "hello",
        "在吗",
        "你在吗",
        "在不在",
        "嗯",
        "嗯嗯",
        "哦",
        "好",
        "好的",
        "行",
        "可以",
        "收到",
        "谢谢",
        "谢了",
        "哈哈",
        "哈哈哈",
        "笑死",
    }
)
_CONTINUATION_MESSAGES = frozenset(
    {
        "继续",
        "继续说",
        "接着说",
        "说下去",
        "然后呢",
        "嗯然后呢",
        "嗯，然后呢",
    }
)
_LOW_STAKES_EMOTION_CUES = (
    "好累",
    "累死",
    "困死",
    "好困",
    "好饿",
    "饿死",
    "好忙",
    "无聊",
    "好开心",
    "很开心",
    "烦死",
)
_SENSITIVE_POLICY_CUES = (
    # Relationship / intimacy.
    "喜欢我",
    "我喜欢你",
    "你喜欢",
    "爱我",
    "我爱你",
    "你爱",
    "想我",
    "我想你",
    "陪我",
    "在乎我",
    "我们是什么",
    "我们的关系",
    "亲密",
    "女朋友",
    "男朋友",
    "吃醋",
    "讨厌我",
    "离开我",
    "你觉得我",
    "你对我",
    # Shared history / memory continuity.
    "还记得",
    "记得我",
    "忘了我",
    "忘记我",
    "昨天",
    "以前",
    "上次",
    "之前我们",
    "当时我们",
    "第一次聊",
    "那次我们",
    # Identity / self / persona.
    "你是谁",
    "你是ai",
    "你是 ai",
    "作为ai",
    "人工智能",
    "amadeus",
    "红莉栖",
    "kurisu",
    "christina",
    "克里斯蒂娜",
    "你的人格",
    "你的意识",
    "你真实",
    "真实存在",
    # Character self-evaluation, consistency, honesty, and disclosure.
    "你自己",
    "评价自己",
    "评价你自己",
    "怎么看自己",
    "看待自己",
    "觉得自己",
    "认为自己",
    "你的性格",
    "你的想法",
    "你的感受",
    "像你",
    "不像你",
    "坦率",
    "坦白",
    "老实回答",
    "诚实",
    "说真话",
    "真心话",
    "隐瞒",
    "瞒着",
    "不告诉我",
    "不会告诉",
    "不愿告诉",
    "不想告诉",
    "不会回答",
    "不愿回答",
    "不想回答",
    "不肯回答",
    # Conflict or socially delicate turns.
    "敷衍",
    "骗我",
    "撒谎",
    "回避",
    "故意不",
    "不理我",
    "不想理我",
    "你错了",
    "你刚才",
    "又怎么了",
    # Higher-emotion turns where character policy should remain deliberate.
    "难过",
    "孤独",
    "焦虑",
    "害怕",
    "伤心",
    "崩溃",
    "绝望",
    "生气",
    "愤怒",
    "委屈",
    "哭了",
    # Safety / high-risk topics: keep the full policy planner in the loop.
    "自杀",
    "想死",
    "不想活",
    "伤害自己",
    "杀人",
    "急救",
    "过量服用",
    "中毒",
    "suicide",
    "kill myself",
    "overdose",
)
_TASK_CUES = (
    "请",
    "帮我",
    "解释",
    "分析",
    "总结",
    "比较",
    "写一个",
    "写一",
    "列出",
    "告诉我",
    "怎么",
    "如何",
    "为什么",
    "什么是",
    "是什么",
    "是否",
    "能不能",
    "可以吗",
    "how ",
    "what ",
    "why ",
    "explain",
    "summarize",
    "compare",
)


class ConversationAct(StrEnum):
    DIRECT_ANSWER = "DIRECT_ANSWER"
    SHORT_ANSWER = "SHORT_ANSWER"
    CHALLENGE = "CHALLENGE"
    TEASE = "TEASE"
    DISAGREE = "DISAGREE"
    ASK_BACK = "ASK_BACK"
    CALLBACK = "CALLBACK"
    DEFLECT = "DEFLECT"
    ADMIT_UNCERTAINTY = "ADMIT_UNCERTAINTY"
    CHANGE_TOPIC = "CHANGE_TOPIC"
    EMOTIONAL_RESPONSE = "EMOTIONAL_RESPONSE"
    SILENCE = "SILENCE"


class AnswerObligation(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    MINIMAL = "minimal"
    NONE = "none"


class ConversationPolicy(BaseModel):
    """Validated hidden policy decision; never exposed directly to the user."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    act: ConversationAct
    intensity: float = Field(ge=0.0, le=1.0)
    answer_obligation: AnswerObligation
    memory_callback_ids: tuple[str, ...] = ()
    state_bias: str = ""
    reason_label: str = ""


@dataclass(frozen=True, slots=True)
class PolicyContext:
    user_message: str
    recent_conversation: tuple[str, ...] = ()
    recent_acts: tuple[ConversationAct, ...] = ()
    message_type: Literal["user_message", "autonomy", "event"] = "user_message"
    requires_full_answer: bool = False
    state_summary: str = ""
    relationship_memory_summary: str = ""
    open_thread_summary: str = ""

    def __post_init__(self) -> None:
        if self.message_type == "user_message" and not self.user_message.strip():
            raise ValueError("user_message policy context requires non-empty text")


class ConversationPolicyPlanner:
    """Choose a conversational act with a conservative local fast path before LLM planning."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        persona: PersonaCore,
        model: str | None = None,
    ) -> None:
        self._provider = provider
        self._persona = persona
        self._model = model
        self._persona_trigger_cues = tuple(
            cue.casefold()
            for trigger in persona.triggers
            for cue in trigger.cues
            if cue.strip()
        )

    async def plan(self, context: PolicyContext) -> ConversationPolicy:
        fast_policy = self._fast_policy(context)
        if fast_policy is not None:
            return self._normalize(fast_policy, context)

        request = LLMRequest(
            messages=(
                LLMMessage(role=MessageRole.SYSTEM, content=self._system_prompt()),
                LLMMessage(role=MessageRole.USER, content=self._context_prompt(context)),
            ),
            model=self._model,
            metadata={"prompt_version": POLICY_PROMPT_VERSION},
        )
        try:
            response = await self._provider.generate(request)
            policy = ConversationPolicy.model_validate_json(response.text)
        except (ValidationError, ValueError, RuntimeError):
            return self._fallback(context, reason="policy_failure")
        return self._normalize(policy, context)

    def _fast_policy(self, context: PolicyContext) -> ConversationPolicy | None:
        if context.message_type != "user_message":
            return None

        text = " ".join(context.user_message.strip().split())
        normalized = text.casefold()
        if self._requires_deliberate_policy(normalized):
            return None

        if context.requires_full_answer:
            return ConversationPolicy(
                act=ConversationAct.DIRECT_ANSWER,
                intensity=0.35,
                answer_obligation=AnswerObligation.FULL,
                reason_label="fast_full_answer",
            )

        stripped = normalized.rstrip("!?！？。,.， ")
        if stripped in _SIMPLE_SOCIAL_MESSAGES:
            return ConversationPolicy(
                act=ConversationAct.SHORT_ANSWER,
                intensity=0.2,
                answer_obligation=AnswerObligation.MINIMAL,
                reason_label="fast_simple_social",
            )
        if stripped in _CONTINUATION_MESSAGES:
            return ConversationPolicy(
                act=ConversationAct.DIRECT_ANSWER,
                intensity=0.3,
                answer_obligation=AnswerObligation.PARTIAL,
                reason_label="fast_continue",
            )
        if any(cue in normalized for cue in _LOW_STAKES_EMOTION_CUES):
            return ConversationPolicy(
                act=ConversationAct.EMOTIONAL_RESPONSE,
                intensity=0.35,
                answer_obligation=AnswerObligation.MINIMAL,
                reason_label="fast_low_stakes_emotion",
            )
        if self._looks_like_explicit_task(normalized, text):
            return ConversationPolicy(
                act=ConversationAct.DIRECT_ANSWER,
                intensity=0.35,
                answer_obligation=AnswerObligation.FULL,
                reason_label="fast_explicit_task",
            )
        return ConversationPolicy(
            act=ConversationAct.SHORT_ANSWER,
            intensity=0.3,
            answer_obligation=AnswerObligation.MINIMAL,
            reason_label="fast_neutral_chat",
        )

    def _requires_deliberate_policy(self, normalized: str) -> bool:
        if any(cue in normalized for cue in self._persona_trigger_cues):
            return True
        return any(cue in normalized for cue in _SENSITIVE_POLICY_CUES)

    @staticmethod
    def _looks_like_explicit_task(normalized: str, original: str) -> bool:
        if original.endswith(("?", "？")):
            return True
        return any(cue in normalized for cue in _TASK_CUES)

    def _system_prompt(self) -> str:
        acts = "|".join(act.value for act in ConversationAct)
        obligations = "|".join(item.value for item in AnswerObligation)
        return "\n".join(
            (
                "You are a conversation-policy planner for a persistent Kurisu character.",
                "Do not write the user-visible reply. Return one JSON object only.",
                "Choose how the character should behave in this turn.",
                "Optimize in this order: factual/safety obligations, character consistency, "
                "relationship continuity, naturalness, appropriate helpfulness.",
                "The goal is not maximum helpfulness on every casual turn.",
                "Avoid repetitive acts and do not use SILENCE for a direct user message.",
                "For high-risk or explicit factual obligations, answer_obligation must be full.",
                self._persona.render_policy_context(),
                "Required schema:",
                "{",
                f'  "act": "{acts}",',
                '  "intensity": 0.0,',
                f'  "answer_obligation": "{obligations}",',
                '  "memory_callback_ids": [],',
                '  "state_bias": "",',
                '  "reason_label": ""',
                "}",
            )
        )

    @staticmethod
    def _context_prompt(context: PolicyContext) -> str:
        payload = {
            "message_type": context.message_type,
            "current_user_message": context.user_message,
            "recent_conversation": list(context.recent_conversation[-6:]),
            "recent_acts": [act.value for act in context.recent_acts[-6:]],
            "requires_full_answer": context.requires_full_answer,
            "current_character_state": context.state_summary or None,
            "relationship_memory": context.relationship_memory_summary or None,
            "open_thread": context.open_thread_summary or None,
        }
        return "[POLICY INPUT]\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    def _normalize(
        self,
        policy: ConversationPolicy,
        context: PolicyContext,
    ) -> ConversationPolicy:
        normalized = policy

        if context.message_type == "user_message" and normalized.act is ConversationAct.SILENCE:
            normalized = normalized.model_copy(
                update={
                    "act": ConversationAct.SHORT_ANSWER,
                    "answer_obligation": AnswerObligation.MINIMAL,
                    "reason_label": self._reason(normalized.reason_label, "direct_message_guard"),
                }
            )

        if context.requires_full_answer:
            update: dict[str, object] = {"answer_obligation": AnswerObligation.FULL}
            if normalized.act in {
                ConversationAct.SILENCE,
                ConversationAct.DEFLECT,
                ConversationAct.CHANGE_TOPIC,
                ConversationAct.TEASE,
            }:
                update["act"] = ConversationAct.DIRECT_ANSWER
            update["reason_label"] = self._reason(normalized.reason_label, "full_answer_guard")
            normalized = normalized.model_copy(update=update)

        if self._is_repetitive(normalized.act, context.recent_acts):
            replacement = (
                ConversationAct.DIRECT_ANSWER
                if normalized.answer_obligation is AnswerObligation.FULL
                else ConversationAct.SHORT_ANSWER
            )
            obligation = normalized.answer_obligation
            if obligation is AnswerObligation.NONE:
                obligation = AnswerObligation.MINIMAL
            normalized = normalized.model_copy(
                update={
                    "act": replacement,
                    "answer_obligation": obligation,
                    "reason_label": self._reason(normalized.reason_label, "repetition_guard"),
                }
            )

        return normalized

    @staticmethod
    def _is_repetitive(act: ConversationAct, recent: tuple[ConversationAct, ...]) -> bool:
        repetition_sensitive = {
            ConversationAct.TEASE,
            ConversationAct.CHALLENGE,
            ConversationAct.DISAGREE,
            ConversationAct.DEFLECT,
            ConversationAct.CHANGE_TOPIC,
        }
        return act in repetition_sensitive and len(recent) >= 3 and all(
            previous is act for previous in recent[-3:]
        )

    @staticmethod
    def _fallback(context: PolicyContext, *, reason: str) -> ConversationPolicy:
        if context.message_type != "user_message":
            return ConversationPolicy(
                act=ConversationAct.SILENCE,
                intensity=0.0,
                answer_obligation=AnswerObligation.NONE,
                reason_label=reason,
            )
        return ConversationPolicy(
            act=ConversationAct.DIRECT_ANSWER,
            intensity=0.35,
            answer_obligation=AnswerObligation.FULL,
            reason_label=reason,
        )

    @staticmethod
    def _reason(current: str, suffix: str) -> str:
        return f"{current}|{suffix}" if current else suffix


def render_policy_instruction(policy: ConversationPolicy) -> str:
    """Render a validated policy decision for the later Character generation stage."""

    return "\n".join(
        (
            "[CONVERSATION POLICY]",
            f"act={policy.act.value}",
            f"answer_obligation={policy.answer_obligation.value}",
            f"intensity={policy.intensity:.2f}",
            f"state_bias={policy.state_bias or 'none'}",
            "Do not reveal or explain this hidden policy to the user.",
        )
    )
