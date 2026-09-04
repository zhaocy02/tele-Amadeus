from pathlib import Path

import pytest

from amadeus_bot.character import (
    AnswerObligation,
    CharacterContextBuilder,
    CharacterContextSources,
    ConversationAct,
    ConversationPolicy,
    load_persona_core,
)
from amadeus_bot.llm import LLMImage, LLMMessage, MessageRole

PERSONA_PATH = Path("profiles/v2/persona_core.json")


def _policy() -> ConversationPolicy:
    return ConversationPolicy(
        act=ConversationAct.DIRECT_ANSWER,
        intensity=0.3,
        answer_obligation=AnswerObligation.FULL,
        reason_label="vision-test",
    )


def test_current_turn_image_is_attached_only_to_current_user_message() -> None:
    image = LLMImage(data=b"jpeg", media_type="image/jpeg")
    builder = CharacterContextBuilder(load_persona_core(PERSONA_PATH))
    context = builder.build(
        policy=_policy(),
        sources=CharacterContextSources(
            current_user_message="[User sent a photo]\nCaption: 看看这个",
            current_user_images=(image,),
            recent_conversation=(LLMMessage(MessageRole.USER, "old text"),),
        ),
    )

    assert context.messages[-1].images == (image,)
    assert all(not message.images for message in context.messages[:-1])
    assert "visual inference as fallible" in context.messages[0].content


def test_recent_conversation_rejects_persisted_image_bytes() -> None:
    with pytest.raises(ValueError, match="must not retain ephemeral image bytes"):
        CharacterContextSources(
            current_user_message="current",
            recent_conversation=(
                LLMMessage(
                    MessageRole.USER,
                    "old",
                    images=(LLMImage(data=b"jpeg", media_type="image/jpeg"),),
                ),
            ),
        )
