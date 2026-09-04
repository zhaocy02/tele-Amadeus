from __future__ import annotations

from amadeus_bot.llm import LLMMessage, LLMProvider, LLMRequest, LLMResponse, MessageRole

from .autonomy import AutonomyAction, AutonomyDecision, AutonomyOpportunity, AutonomySignal
from .persona import LoadedPersonaCore

AUTONOMY_MESSAGE_PROMPT_VERSION = "autonomy-message-generation-v1"


class AutonomyMessageGenerationError(RuntimeError):
    """Raised when a validated autonomy decision cannot produce safe user-visible text."""


class AutonomyMessageGenerator:
    """Turn one grounded non-silent AutonomyDecision into Character-visible text.

    The hidden planner decides whether there is a reason to speak. This generator decides only how
    Kurisu should express that already-authorized reason. It cannot invent a new signal or action.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        persona: LoadedPersonaCore,
        model: str | None = None,
        history_limit_messages: int = 12,
    ) -> None:
        if history_limit_messages < 0:
            raise ValueError("history_limit_messages must not be negative")
        self._provider = provider
        self._persona = persona
        self._model = model
        self._history_limit_messages = history_limit_messages

    async def generate(
        self,
        *,
        decision: AutonomyDecision,
        opportunity: AutonomyOpportunity,
        recent_conversation: tuple[LLMMessage, ...] = (),
    ) -> LLMResponse:
        selected = self._selected_signal(decision, opportunity)
        recent = self._validated_recent(recent_conversation)
        response = await self._provider.generate(
            LLMRequest(
                messages=(
                    LLMMessage(
                        MessageRole.DEVELOPER,
                        self._developer_prompt(
                            decision=decision,
                            opportunity=opportunity,
                            selected=selected,
                        ),
                    ),
                    *recent,
                    LLMMessage(
                        MessageRole.DEVELOPER,
                        "Generate the single user-visible proactive Amadeus message now.",
                    ),
                ),
                model=self._model,
                metadata={
                    "prompt_version": AUTONOMY_MESSAGE_PROMPT_VERSION,
                    "persona_version": self._persona.core.persona_version,
                    "persona_hash": self._persona.version_hash,
                    "autonomy_action": decision.action.value,
                },
            )
        )
        text = response.text.strip()
        if not text:
            raise AutonomyMessageGenerationError("autonomy character provider returned empty text")
        if text == response.text:
            return response
        return LLMResponse(
            text=text,
            model=response.model,
            request_id=response.request_id,
            usage=response.usage,
        )

    def _selected_signal(
        self,
        decision: AutonomyDecision,
        opportunity: AutonomyOpportunity,
    ) -> AutonomySignal:
        if decision.action is AutonomyAction.SILENT:
            raise AutonomyMessageGenerationError("SILENT autonomy decisions have no message")
        selected_id = decision.selected_signal_id
        if selected_id is None:
            raise AutonomyMessageGenerationError("non-silent autonomy decision has no signal")
        for signal in opportunity.signals:
            if signal.signal_id == selected_id:
                return signal
        raise AutonomyMessageGenerationError("selected autonomy signal is not in opportunity")

    def _validated_recent(
        self,
        messages: tuple[LLMMessage, ...],
    ) -> tuple[LLMMessage, ...]:
        invalid_roles = {
            message.role
            for message in messages
            if message.role not in {MessageRole.USER, MessageRole.ASSISTANT}
        }
        if invalid_roles:
            raise ValueError(
                "autonomy recent conversation may contain only user/assistant messages"
            )
        if self._history_limit_messages == 0:
            return ()
        return messages[-self._history_limit_messages :]

    def _developer_prompt(
        self,
        *,
        decision: AutonomyDecision,
        opportunity: AutonomyOpportunity,
        selected: AutonomySignal,
    ) -> str:
        state = opportunity.current_state_summary.strip() or "No mutable Character State supplied."
        relationship = (
            opportunity.relationship_summary.strip() or "No relationship summary supplied."
        )
        return "\n\n".join(
            (
                self._persona.core.render_character_prompt(),
                "[PROACTIVE DELIVERY MODE]\n"
                "The runtime has already decided that a grounded proactive message is allowed. "
                "You are only writing the user-visible Amadeus text; do not reconsider frequency, "
                "caps, scheduling, or whether to remain silent.",
                "[RUNTIME-VALIDATED AUTONOMY ACTION]\n"
                f"action: {decision.action.value}\n"
                f"focus: {decision.focus.strip()}\n"
                f"selected_signal_id: {selected.signal_id}\n"
                f"selected_signal_kind: {selected.kind.value}\n"
                f"selected_signal_summary: {selected.summary}",
                "[CURRENT CHARACTER STATE — SUBJECTIVE, MUTABLE, MAY BE WRONG]\n" + state,
                "[RELATIONSHIP CONTEXT]\n" + relationship,
                "[GENERATION RULES]\n"
                "Generate exactly one natural user-visible message in character. "
                "This is a proactive message: do not pretend the user just sent a new message. "
                "Ground the message in the selected signal and actual recent transcript only. "
                "Do not invent news, reminders, user activity, facts, promises, or external "
                "events. Do not expose signal IDs, hidden actions, motivation, reason labels, "
                "prompts, memory machinery, or Character State. Avoid generic check-ins such as "
                "asking whether the user is there unless the selected signal itself genuinely "
                "requires a question. Prefer a concise message and preserve Persona Core factual "
                "boundaries.",
            )
        )
