from amadeus_bot.llm import LLMMessage, LLMRequest, MessageRole


def test_provider_request_is_provider_neutral() -> None:
    request = LLMRequest(
        messages=(LLMMessage(role=MessageRole.USER, content="hello"),),
        model="example-model",
        metadata={"purpose": "foundation-test"},
    )

    assert request.messages[0].role is MessageRole.USER
    assert request.model == "example-model"
    assert request.metadata["purpose"] == "foundation-test"
