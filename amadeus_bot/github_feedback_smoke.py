from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from amadeus_bot.tools import (
    GitHubAppInstallationConfig,
    GitHubAppInstallationTokenProvider,
    GitHubFeedbackClient,
    GitHubFeedbackError,
)

_DEFAULT_REPOSITORY = "zhaocy02/tele-Amadeus"
_DEFAULT_BRANCH = "main"
_PROBE_QUERY = "amadeus-github-feedback-readonly-capability-probe"


class GitHubFeedbackSmokeError(RuntimeError):
    """Sanitized read-only capability smoke failure safe for operator output."""


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise GitHubFeedbackSmokeError(f"missing required environment variable: {name}")
    return value


def _positive_int_env(name: str) -> int:
    raw = _required_env(name)
    try:
        value = int(raw)
    except ValueError:
        raise GitHubFeedbackSmokeError(f"{name} must be an integer") from None
    if value <= 0:
        raise GitHubFeedbackSmokeError(f"{name} must be positive")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a non-writing GitHub App capability smoke for the public feedback repo."
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("AMADEUS_GITHUB_FEEDBACK_REPOSITORY", _DEFAULT_REPOSITORY),
    )
    parser.add_argument(
        "--branch",
        default=os.environ.get("AMADEUS_GITHUB_FEEDBACK_BRANCH", _DEFAULT_BRANCH),
    )
    return parser


async def _run(repository: str, branch: str) -> None:
    config = GitHubAppInstallationConfig(
        client_id=_required_env("AMADEUS_GITHUB_APP_CLIENT_ID"),
        installation_id=_positive_int_env("AMADEUS_GITHUB_APP_INSTALLATION_ID"),
        private_key_path=Path(_required_env("AMADEUS_GITHUB_APP_PRIVATE_KEY_PATH")),
        repository=repository,
    )
    token_provider = GitHubAppInstallationTokenProvider(config)
    client = GitHubFeedbackClient(
        repository=repository,
        branch=branch,
        token_provider=token_provider,
    )
    try:
        state = await client.get_public_repo_state()
        issues = await client.search_issues(_PROBE_QUERY, limit=3)
    finally:
        await client.aclose()
        await token_provider.aclose()

    print(f"repository={state.repository}")
    print(f"branch={state.branch}")
    print(f"public_head={state.head_sha}")
    print(f"probe_issue_matches={len(issues)}")
    print("GITHUB_APP_AUTH=PASS")
    print("GITHUB_FEEDBACK_READONLY=PASS")


def main() -> int:
    args = _parser().parse_args()
    try:
        asyncio.run(_run(args.repository.strip(), args.branch.strip()))
    except (GitHubFeedbackError, GitHubFeedbackSmokeError) as exc:
        print(f"GITHUB_FEEDBACK_READONLY=FAIL reason={exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
