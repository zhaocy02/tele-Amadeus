from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

_RED_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKElEQVR4nO3NsQ0AAAzCMP5/"
    "un0CNkuZ41wybXsHAAAAAAAAAAAAxR4yw/wuPL6QkAAAAABJRU5ErkJggg=="
)
_PROMPT = (
    "Look at the attached image. Reply with exactly RED if the image's dominant color is red. "
    "Otherwise reply with exactly NOT_RED. Do not add punctuation or explanation."
)


class VisionCapabilityError(RuntimeError):
    """Sanitized provider capability failure safe to paste into an issue or chat."""


@dataclass(frozen=True, slots=True)
class VisionCapabilityResult:
    response_text: str
    model: str
    request_id: str | None

    @property
    def passed(self) -> bool:
        return self.response_text.strip().upper() == "RED"


class VisionCapabilityProbe:
    """Probe the current Responses-compatible provider without involving Telegram runtime."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("provider base URL must use http:// or https://")
        if not model.strip():
            raise ValueError("provider model must not be empty")
        if timeout_seconds <= 0 or timeout_seconds > 600:
            raise ValueError("timeout must be between 0 and 600 seconds")
        self._base_url = normalized
        self._api_key = api_key.strip() if api_key else None
        self._model = model.strip()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, trust_env=False)

    async def run(self) -> VisionCapabilityResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": _PROMPT},
                        {"type": "input_image", "image_url": _RED_PNG_DATA_URL},
                    ],
                }
            ],
            "store": False,
        }
        try:
            response = await self._client.post(
                self._endpoint("responses"),
                headers=self._headers(),
                json=payload,
            )
        except httpx.TimeoutException:
            raise VisionCapabilityError("provider vision smoke timed out") from None
        except httpx.HTTPError:
            raise VisionCapabilityError("provider vision smoke request failed") from None

        if response.status_code >= 400:
            error_type, error_code = self._extract_error_identity(response)
            suffix = ""
            if error_type:
                suffix += f" error_type={error_type}"
            if error_code:
                suffix += f" error_code={error_code}"
            raise VisionCapabilityError(
                f"provider vision smoke returned HTTP {response.status_code}{suffix}"
            )

        try:
            raw: object = response.json()
        except ValueError:
            raise VisionCapabilityError("provider vision smoke returned invalid JSON") from None
        if not isinstance(raw, dict):
            raise VisionCapabilityError("provider vision smoke returned an invalid response object")
        data: Mapping[str, object] = raw
        text = self._extract_output_text(data)
        model_value = data.get("model")
        request_id_value = data.get("id")
        return VisionCapabilityResult(
            response_text=text,
            model=model_value if isinstance(model_value, str) else self._model,
            request_id=request_id_value if isinstance(request_id_value, str) else None,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _endpoint(self, resource: str) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/{resource}"
        return f"{self._base_url}/v1/{resource}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _extract_output_text(data: Mapping[str, object]) -> str:
        output = data.get("output")
        if not isinstance(output, list):
            raise VisionCapabilityError("provider vision smoke response contains no output")
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        joined = "".join(parts).strip()
        if not joined:
            raise VisionCapabilityError("provider vision smoke response contains no text output")
        return joined

    @staticmethod
    def _extract_error_identity(response: httpx.Response) -> tuple[str | None, str | None]:
        try:
            raw: object = response.json()
        except ValueError:
            return None, None
        if not isinstance(raw, dict):
            return None, None
        error = raw.get("error")
        if not isinstance(error, dict):
            return None, None
        error_type = error.get("type")
        error_code = error.get("code")
        return (
            error_type if isinstance(error_type, str) else None,
            error_code if isinstance(error_code, str) else None,
        )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise VisionCapabilityError(f"missing required environment variable: {name}")
    return value


def _timeout_from_env() -> float:
    raw = os.environ.get("AMADEUS_REQUEST_TIMEOUT_SECONDS", "120").strip() or "120"
    try:
        value = float(raw)
    except ValueError:
        raise VisionCapabilityError("AMADEUS_REQUEST_TIMEOUT_SECONDS must be numeric") from None
    if value <= 0 or value > 600:
        raise VisionCapabilityError(
            "AMADEUS_REQUEST_TIMEOUT_SECONDS must be between 0 and 600"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Probe whether the configured Responses-compatible provider accepts input_image. "
            "The command does not initialize Telegram or access Amadeus runtime data."
        )
    )


async def _run_from_environment() -> int:
    probe = VisionCapabilityProbe(
        base_url=_required_env("AMADEUS_PROVIDER_BASE_URL"),
        api_key=os.environ.get("AMADEUS_PROVIDER_API_KEY", "").strip() or None,
        model=os.environ.get("AMADEUS_PROVIDER_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol",
        timeout_seconds=_timeout_from_env(),
    )
    try:
        result = await probe.run()
    finally:
        await probe.aclose()

    print("Amadeus Phase 5.5 provider vision capability smoke")
    print(f"model={result.model}")
    print(f"response={result.response_text.strip()[:80]}")
    if result.passed:
        print("VISION_CAPABILITY=PASS")
        return 0
    print("VISION_CAPABILITY=INCONCLUSIVE")
    return 1


def main() -> None:
    _parser().parse_args()
    try:
        raise SystemExit(asyncio.run(_run_from_environment()))
    except (ValueError, VisionCapabilityError) as exc:
        print("Amadeus Phase 5.5 provider vision capability smoke")
        print(f"VISION_CAPABILITY=FAIL reason={exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
