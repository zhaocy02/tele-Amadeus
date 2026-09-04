from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .provider import (
    LLMImage,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
    MessageRole,
)


class ProviderError(RuntimeError):
    """A sanitized provider failure safe to surface to the Telegram layer."""


@dataclass(frozen=True, slots=True)
class _ResponsesContinuation:
    """Opaque Responses items that must be replayed while a function loop is active."""

    items: tuple[dict[str, Any], ...]


class ResponsesAPIProvider:
    """OpenAI-compatible Responses API adapter used by the current provider profiles.

    The domain-facing interface remains ``LLMProvider``. Concrete upstreams are implementation
    details here, which keeps Character Runtime code independent from provider identity.

    ``LLMRequest.metadata`` is internal runtime trace metadata. It is intentionally not serialized
    onto the wire request because compatibility surfaces do not universally accept the upstream
    ``metadata`` request parameter.

    Function-call continuation state is opaque to Character Runtime. The adapter replays prior
    Responses output items, including reasoning items when present, before appending function
    outputs. This keeps the tool loop stateless at the HTTP boundary without leaking provider
    response structure into Character code.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        default_model: str,
        reasoning_effort: str = "high",
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._default_model = default_model
        self._reasoning_effort = reasoning_effort
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, trust_env=False)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        if not request.messages:
            raise ValueError("LLM request must contain at least one message")

        instructions: list[str] = []
        input_items: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER}:
                instructions.append(message.content)
                continue
            input_items.append(
                {
                    "role": message.role.value,
                    "content": self._wire_content(message.content, message.images),
                }
            )

        if not input_items:
            raise ValueError("LLM request must contain at least one user or assistant message")

        continuation_items = self._continuation_items(request.continuation)
        tool_result_items = tuple(
            {
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": result.output,
            }
            for result in request.tool_results
        )
        input_items.extend(dict(item) for item in continuation_items)
        input_items.extend(dict(item) for item in tool_result_items)

        payload: dict[str, Any] = {
            "model": request.model or self._default_model,
            "input": input_items,
            "store": False,
        }
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if self._reasoning_effort != "none":
            payload["reasoning"] = {"effort": self._reasoning_effort}
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in request.tools
            ]
            payload["tool_choice"] = "auto"

        try:
            response = await self._client.post(
                self._endpoint("responses"),
                headers=self._headers(),
                json=payload,
            )
        except httpx.TimeoutException:
            raise ProviderError("provider request timed out") from None
        except httpx.HTTPError:
            raise ProviderError("provider request could not be completed") from None

        if response.status_code >= 400:
            raise ProviderError(f"provider returned HTTP {response.status_code}")

        raw: object = response.json()
        if not isinstance(raw, dict):
            raise ProviderError("provider returned an invalid response object")
        data: Mapping[str, object] = raw
        output_items = self._extract_output_items(data)
        text = self._extract_output_text(output_items)
        tool_calls = self._extract_tool_calls(output_items)
        if not text and not tool_calls:
            raise ProviderError("provider response contains neither text nor function calls")

        model_value = data.get("model")
        request_id_value = data.get("id")
        usage = self._extract_usage(data.get("usage"))
        model = (
            model_value
            if isinstance(model_value, str)
            else request.model or self._default_model
        )
        continuation: object | None = None
        if tool_calls:
            continuation = _ResponsesContinuation(
                items=(
                    *continuation_items,
                    *tool_result_items,
                    *output_items,
                )
            )
        return LLMResponse(
            text=text,
            model=model,
            request_id=request_id_value if isinstance(request_id_value, str) else None,
            usage=usage,
            tool_calls=tool_calls,
            continuation=continuation,
        )

    async def healthcheck(self) -> None:
        """Verify that the configured provider accepts authenticated requests without generating."""

        try:
            response = await self._client.get(self._endpoint("models"), headers=self._headers())
        except httpx.TimeoutException:
            raise ProviderError("provider healthcheck timed out") from None
        except httpx.HTTPError:
            raise ProviderError("provider healthcheck could not be completed") from None
        if response.status_code >= 400:
            raise ProviderError(f"provider healthcheck returned HTTP {response.status_code}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _endpoint(self, resource: str) -> str:
        # CPA/DeepSeek historically use either a host root or /v1. Ark exposes its OpenAI-compatible
        # Responses surface under /api/v3. Respect any explicit trailing /vN path instead of
        # inserting another /v1 segment.
        version_segment = self._base_url.rsplit("/", 1)[-1]
        if len(version_segment) > 1 and version_segment[0] == "v" and version_segment[1:].isdigit():
            return f"{self._base_url}/{resource}"
        return f"{self._base_url}/v1/{resource}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _wire_content(text: str, images: tuple[LLMImage, ...]) -> str | list[dict[str, str]]:
        if not images:
            return text
        parts: list[dict[str, str]] = []
        if text.strip():
            parts.append({"type": "input_text", "text": text})
        for image in images:
            encoded = base64.b64encode(image.data).decode("ascii")
            parts.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{image.media_type};base64,{encoded}",
                }
            )
        return parts

    @staticmethod
    def _continuation_items(continuation: object | None) -> tuple[dict[str, Any], ...]:
        if continuation is None:
            return ()
        if not isinstance(continuation, _ResponsesContinuation):
            raise ProviderError("provider continuation state is incompatible")
        return continuation.items

    @staticmethod
    def _extract_output_items(data: Mapping[str, object]) -> tuple[dict[str, Any], ...]:
        output = data.get("output")
        if not isinstance(output, list):
            raise ProviderError("provider response does not contain output")
        items = tuple(item for item in output if isinstance(item, dict))
        if not items:
            raise ProviderError("provider response contains no usable output items")
        return items

    @staticmethod
    def _extract_output_text(output_items: tuple[dict[str, Any], ...]) -> str:
        text_parts: list[str] = []
        for item in output_items:
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
        return "".join(text_parts).strip()

    @staticmethod
    def _extract_tool_calls(
        output_items: tuple[dict[str, Any], ...],
    ) -> tuple[LLMToolCall, ...]:
        calls: list[LLMToolCall] = []
        for item in output_items:
            if item.get("type") != "function_call":
                continue
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id.strip():
                raise ProviderError("provider function call has no call_id")
            if not isinstance(name, str) or not name.strip():
                raise ProviderError("provider function call has no name")
            raw_arguments = item.get("arguments")
            if isinstance(raw_arguments, str):
                try:
                    arguments: object = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    raise ProviderError(
                        "provider function call arguments are invalid JSON"
                    ) from None
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise ProviderError("provider function call arguments are missing")
            if not isinstance(arguments, dict):
                raise ProviderError("provider function call arguments must be an object")
            item_id = item.get("id")
            calls.append(
                LLMToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=arguments,
                    item_id=item_id if isinstance(item_id, str) else None,
                )
            )
        return tuple(calls)

    @staticmethod
    def _extract_usage(raw: object) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = raw.get(key)
            if isinstance(value, int):
                result[key] = value
        return result
