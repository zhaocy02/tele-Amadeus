from __future__ import annotations

import argparse
import asyncio
import os
from urllib.parse import urlsplit

from amadeus_bot.tools import CPAWebSearchProvider, WebSearchError

_SMOKE_QUERY = "OpenAI official current models"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe CPA-hosted native web_search without initializing Telegram or runtime data."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        choices=range(1, 6),
        metavar="1-5",
        help="maximum source results to retain (default: 3)",
    )
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WebSearchError(f"missing required environment variable: {name}")
    return value


def _timeout_from_env() -> float:
    raw = os.environ.get("AMADEUS_REQUEST_TIMEOUT_SECONDS", "120").strip() or "120"
    try:
        value = float(raw)
    except ValueError:
        raise WebSearchError("AMADEUS_REQUEST_TIMEOUT_SECONDS must be numeric") from None
    if value <= 0 or value > 600:
        raise WebSearchError("AMADEUS_REQUEST_TIMEOUT_SECONDS must be between 0 and 600")
    return value


async def _run(*, limit: int) -> int:
    provider = CPAWebSearchProvider(
        base_url=_required_env("AMADEUS_PROVIDER_BASE_URL"),
        api_key=os.environ.get("AMADEUS_PROVIDER_API_KEY", "").strip() or None,
        model=os.environ.get("AMADEUS_PROVIDER_MODEL", "gpt-5.6-sol").strip() or "gpt-5.6-sol",
        reasoning_effort="low",
        timeout_seconds=_timeout_from_env(),
    )
    try:
        evidence = await provider.search(_SMOKE_QUERY, limit=limit)
    finally:
        await provider.aclose()

    print("Amadeus CPA native web-search capability smoke")
    print(f"provider={evidence.provider}")
    print(f"sources={len(evidence.results)}")
    print(f"summary_chars={len(evidence.summary)}")
    for index, result in enumerate(evidence.results, start=1):
        domain = urlsplit(result.url).hostname or "unknown"
        print(f"source_{index}_domain={domain}")
    print("WEB_SEARCH_CAPABILITY=PASS")
    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_run(limit=args.limit)))
    except (ValueError, WebSearchError) as exc:
        print("Amadeus CPA native web-search capability smoke")
        print(f"WEB_SEARCH_CAPABILITY=FAIL reason={exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
