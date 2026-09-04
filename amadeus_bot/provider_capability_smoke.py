from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass

from amadeus_bot.llm import (
    LLMMessage,
    LLMRequest,
    LLMToolDefinition,
    LLMToolResult,
    MessageRole,
    ProviderError,
    ResponsesAPIProvider,
)
from amadeus_bot.tools import (
    CPAWebSearchProvider,
    ResponsesWebSearchProvider,
    WebSearchError,
    WebSearchProvider,
)
from amadeus_bot.vision_smoke import VisionCapabilityError, VisionCapabilityProbe

_TEXT_EXPECTED = "AMADEUS_PROVIDER_OK"
_TOOL_ARGUMENT_EXPECTED = "AMADEUS_TOOL_PING"
_TOOL_RESULT_EXPECTED = "AMADEUS_TOOL_OK"
_WEB_QUERY = "OpenAI official current models"
_TOOL_DEFINITION = LLMToolDefinition(
    name="runtime_probe",
    description="Return a deterministic runtime probe payload. Use only for capability testing.",
    parameters={
        "type": "object",
        "properties": {
            "value": {"type": "string"},
        },
        "required": ["value"],
        "additionalProperties": False,
    },
)


class ProviderCapabilityError(RuntimeError):
    """Sanitized multi-provider capability failure safe for operator output."""


@dataclass(frozen=True, slots=True)
class ProviderSmokeConfig:
    name: str
    base_url: str
    api_key: str
    text_model: str
    vision_model: str
    reasoning_effort: str
    timeout_seconds: float


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderCapabilityError(f"missing required environment variable: {name}")
    return value


def _timeout_from_env() -> float:
    raw = os.environ.get("AMADEUS_REQUEST_TIMEOUT_SECONDS", "120").strip() or "120"
    try:
        value = float(raw)
    except ValueError:
        raise ProviderCapabilityError("AMADEUS_REQUEST_TIMEOUT_SECONDS must be numeric") from None
    if value <= 0 or value > 600:
        raise ProviderCapabilityError(
            "AMADEUS_REQUEST_TIMEOUT_SECONDS must be between 0 and 600"
        )
    return value


def _config_from_environment(provider: str) -> ProviderSmokeConfig:
    if provider == "cpa":
        model = os.environ.get("AMADEUS_PROVIDER_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol"
        return ProviderSmokeConfig(
            name="cpa",
            base_url=_required_env("AMADEUS_PROVIDER_BASE_URL"),
            api_key=_required_env("AMADEUS_PROVIDER_API_KEY"),
            text_model=model,
            vision_model=model,
            reasoning_effort=(
                os.environ.get("AMADEUS_PROVIDER_REASONING_EFFORT", "high").strip() or "high"
            ),
            timeout_seconds=_timeout_from_env(),
        )
    if provider == "deepseek":
        return ProviderSmokeConfig(
            name="deepseek",
            base_url=(
                os.environ.get("AMADEUS_DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
                or "https://api.deepseek.com"
            ),
            api_key=_required_env("AMADEUS_DEEPSEEK_API_KEY"),
            text_model=(
                os.environ.get("AMADEUS_DEEPSEEK_MODEL", "deepseek-v4-pro").strip()
                or "deepseek-v4-pro"
            ),
            vision_model=(
                os.environ.get(
                    "AMADEUS_DEEPSEEK_VISION_MODEL",
                    "deepseek-v4-flash-vision-exp",
                ).strip()
                or "deepseek-v4-flash-vision-exp"
            ),
            reasoning_effort=(
                os.environ.get("AMADEUS_DEEPSEEK_REASONING_EFFORT", "high").strip()
                or "high"
            ),
            timeout_seconds=_timeout_from_env(),
        )
    raise ProviderCapabilityError(f"unsupported provider: {provider}")


def _llm_provider(config: ProviderSmokeConfig) -> ResponsesAPIProvider:
    return ResponsesAPIProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        default_model=config.text_model,
        reasoning_effort=config.reasoning_effort,
        timeout_seconds=config.timeout_seconds,
    )


async def _probe_text(config: ProviderSmokeConfig) -> None:
    provider = _llm_provider(config)
    try:
        response = await provider.generate(
            LLMRequest(
                messages=(
                    LLMMessage(
                        role=MessageRole.USER,
                        content=(
                            f"Reply with exactly {_TEXT_EXPECTED}. "
                            "Do not add punctuation or explanation."
                        ),
                    ),
                ),
                model=config.text_model,
            )
        )
    finally:
        await provider.aclose()

    normalized = response.text.strip()
    print(f"text_model={response.model}")
    print(f"text_response={normalized[:80]}")
    if normalized != _TEXT_EXPECTED:
        raise ProviderCapabilityError("text provider returned an unexpected smoke response")
    print("TEXT_CAPABILITY=PASS")


async def _probe_tool(config: ProviderSmokeConfig) -> None:
    provider = _llm_provider(config)
    user_message = LLMMessage(
        role=MessageRole.USER,
        content=(
            f"You must call the runtime_probe function exactly once with value "
            f"{_TOOL_ARGUMENT_EXPECTED}. Do not answer before the function result. After receiving "
            f"the function result, reply with exactly {_TOOL_RESULT_EXPECTED}."
        ),
    )
    try:
        first = await provider.generate(
            LLMRequest(
                messages=(user_message,),
                model=config.text_model,
                tools=(_TOOL_DEFINITION,),
            )
        )
        if len(first.tool_calls) != 1:
            raise ProviderCapabilityError(
                f"tool provider returned {len(first.tool_calls)} function calls instead of one"
            )
        call = first.tool_calls[0]
        if call.name != _TOOL_DEFINITION.name:
            raise ProviderCapabilityError("tool provider requested an unexpected function")
        if call.arguments.get("value") != _TOOL_ARGUMENT_EXPECTED:
            raise ProviderCapabilityError("tool provider returned unexpected function arguments")
        if first.continuation is None:
            raise ProviderCapabilityError("tool provider returned no continuation state")

        second = await provider.generate(
            LLMRequest(
                messages=(user_message,),
                model=config.text_model,
                tool_results=(
                    LLMToolResult(
                        call_id=call.call_id,
                        output=json.dumps(
                            {"ok": True, "token": _TOOL_RESULT_EXPECTED},
                            separators=(",", ":"),
                        ),
                    ),
                ),
                continuation=first.continuation,
            )
        )
    finally:
        await provider.aclose()

    normalized = second.text.strip()
    print(f"tool_model={second.model}")
    print(f"tool_call={call.name}")
    print(f"tool_response={normalized[:80]}")
    if second.tool_calls:
        raise ProviderCapabilityError("tool provider requested another function after tool output")
    if normalized != _TOOL_RESULT_EXPECTED:
        raise ProviderCapabilityError("tool provider returned an unexpected continuation response")
    print("FUNCTION_TOOL_CAPABILITY=PASS")


async def _probe_vision(config: ProviderSmokeConfig) -> None:
    probe = VisionCapabilityProbe(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.vision_model,
        timeout_seconds=config.timeout_seconds,
    )
    try:
        result = await probe.run()
    finally:
        await probe.aclose()

    print(f"vision_model={result.model}")
    print(f"vision_response={result.response_text.strip()[:80]}")
    if not result.passed:
        raise ProviderCapabilityError("vision provider did not identify the smoke image")
    print("VISION_CAPABILITY=PASS")


def _web_provider(config: ProviderSmokeConfig) -> WebSearchProvider:
    if config.name == "cpa":
        return CPAWebSearchProvider(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.text_model,
            reasoning_effort="low",
            timeout_seconds=min(config.timeout_seconds, 60.0),
        )
    return ResponsesWebSearchProvider(
        provider_name="deepseek-native-web-search",
        provider_label="DeepSeek",
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.text_model,
        reasoning_effort="low",
        timeout_seconds=min(config.timeout_seconds, 60.0),
    )


async def _probe_web(config: ProviderSmokeConfig) -> None:
    provider = _web_provider(config)
    try:
        evidence = await provider.search(_WEB_QUERY, limit=3)
    finally:
        await provider.aclose()

    print(f"web_provider={evidence.provider}")
    print(f"web_sources={len(evidence.results)}")
    if not evidence.results:
        raise ProviderCapabilityError("web search returned no source evidence")
    print("WEB_SEARCH_CAPABILITY=PASS")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe one configured Amadeus provider without initializing Telegram or runtime data."
        )
    )
    parser.add_argument("--provider", required=True, choices=("cpa", "deepseek"))
    parser.add_argument(
        "--capability",
        choices=("text", "tool", "vision", "web", "all"),
        default="all",
        help="capability to probe (default: all)",
    )
    return parser


async def _run(provider: str, capability: str) -> int:
    config = _config_from_environment(provider)
    print("Amadeus multi-provider capability smoke")
    print(f"provider={config.name}")

    if capability in {"text", "all"}:
        await _probe_text(config)
    if capability in {"tool", "all"}:
        await _probe_tool(config)
    if capability in {"vision", "all"}:
        await _probe_vision(config)
    if capability in {"web", "all"}:
        await _probe_web(config)

    print("PROVIDER_CAPABILITY=PASS")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(args.provider, args.capability)))
    except (
        ProviderCapabilityError,
        ProviderError,
        VisionCapabilityError,
        WebSearchError,
        ValueError,
    ) as exc:
        reason = str(exc)
    except Exception as exc:
        reason = type(exc).__name__

    print("Amadeus multi-provider capability smoke")
    print(f"PROVIDER_CAPABILITY=FAIL reason={reason}")
    raise SystemExit(1) from None


if __name__ == "__main__":
    main()
