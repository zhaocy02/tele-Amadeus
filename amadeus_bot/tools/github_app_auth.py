from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
from jwt.exceptions import PyJWTError

from .github_feedback import GitHubFeedbackAPIError, GitHubFeedbackConfigurationError

_GITHUB_API_VERSION = "2026-03-10"


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationConfig:
    """Backend-only GitHub App installation credentials and hard capability scope."""

    client_id: str
    installation_id: int
    private_key_path: Path
    repository: str
    api_base_url: str = "https://api.github.com"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.client_id.strip():
            raise GitHubFeedbackConfigurationError("GitHub App client_id must not be empty")
        if self.installation_id <= 0:
            raise GitHubFeedbackConfigurationError(
                "GitHub App installation_id must be positive"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository.strip()):
            raise GitHubFeedbackConfigurationError("repository must use owner/name form")
        if not self.api_base_url.startswith(("http://", "https://")):
            raise GitHubFeedbackConfigurationError("GitHub API base URL must use HTTP(S)")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 120:
            raise GitHubFeedbackConfigurationError(
                "GitHub App timeout_seconds must be between 0 and 120"
            )


class GitHubAppInstallationTokenProvider:
    """Mint and cache repository-scoped GitHub App installation access tokens."""

    def __init__(
        self,
        config: GitHubAppInstallationConfig,
        *,
        clock: Callable[[], datetime] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cached_token: str | None = None
        self._cached_expires_at: datetime | None = None
        self._client = httpx.AsyncClient(
            base_url=config.api_base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_token(self) -> str:
        now = self._utc_now()
        if (
            self._cached_token is not None
            and self._cached_expires_at is not None
            and now + timedelta(minutes=5) < self._cached_expires_at
        ):
            return self._cached_token

        token, expires_at = await self._mint_token(now)
        self._cached_token = token
        self._cached_expires_at = expires_at
        return token

    async def _mint_token(self, now: datetime) -> tuple[str, datetime]:
        app_jwt = self._build_app_jwt(now)
        repository_name = self._config.repository.split("/", maxsplit=1)[1]
        response = await self._client.post(
            f"/app/installations/{self._config.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
            json={
                "repositories": [repository_name],
                "permissions": {
                    "contents": "read",
                    "issues": "write",
                    "metadata": "read",
                },
            },
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="mint installation token",
            )

        payload = response.json()
        token = payload.get("token")
        expires_at_raw = payload.get("expires_at")
        if not isinstance(token, str) or not token.strip():
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse installation token",
            )
        if not isinstance(expires_at_raw, str):
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse installation token expiry",
            )
        expires_at = _parse_github_datetime(expires_at_raw)
        self._validate_minted_scope(payload, response.status_code)
        return token.strip(), expires_at

    def _build_app_jwt(self, now: datetime) -> str:
        key_path = self._config.private_key_path.expanduser()
        try:
            private_key = key_path.read_bytes()
        except OSError as exc:
            raise GitHubFeedbackConfigurationError(
                "unable to read GitHub App private key file"
            ) from exc
        if not private_key:
            raise GitHubFeedbackConfigurationError("GitHub App private key file is empty")

        issued_at = int(now.timestamp()) - 60
        expires_at = int(now.timestamp()) + 9 * 60
        try:
            encoded = jwt.encode(
                {
                    "iat": issued_at,
                    "exp": expires_at,
                    "iss": self._config.client_id.strip(),
                },
                private_key,
                algorithm="RS256",
            )
        except (PyJWTError, ValueError, TypeError) as exc:
            raise GitHubFeedbackConfigurationError(
                "unable to sign GitHub App JWT with configured private key"
            ) from exc
        if not isinstance(encoded, str) or not encoded:
            raise GitHubFeedbackConfigurationError("GitHub App JWT encoder returned no token")
        return encoded

    def _validate_minted_scope(self, payload: object, status_code: int) -> None:
        if not isinstance(payload, dict):
            raise GitHubFeedbackAPIError(
                status_code=status_code,
                operation="parse installation token scope",
            )

        permissions = payload.get("permissions")
        if not isinstance(permissions, dict):
            raise GitHubFeedbackAPIError(
                status_code=status_code,
                operation="parse installation token permissions",
            )
        expected = {
            "contents": "read",
            "issues": "write",
            "metadata": "read",
        }
        for name, level in expected.items():
            if permissions.get(name) != level:
                raise GitHubFeedbackConfigurationError(
                    "GitHub App installation token has an unexpected permission scope"
                )
        for name, level in permissions.items():
            if level == "write" and name != "issues":
                raise GitHubFeedbackConfigurationError(
                    "GitHub App installation token exposes unexpected write permission"
                )

        repositories = payload.get("repositories")
        if not isinstance(repositories, list):
            raise GitHubFeedbackAPIError(
                status_code=status_code,
                operation="parse installation token repositories",
            )
        full_names = {
            item.get("full_name")
            for item in repositories
            if isinstance(item, dict) and isinstance(item.get("full_name"), str)
        }
        if full_names != {self._config.repository}:
            raise GitHubFeedbackConfigurationError(
                "GitHub App installation token is not pinned to the configured repository"
            )

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise GitHubFeedbackConfigurationError("GitHub App clock must be timezone-aware")
        return now.astimezone(UTC)


def _parse_github_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GitHubFeedbackConfigurationError(
            "GitHub returned an invalid installation token expiry"
        ) from exc
    if parsed.tzinfo is None:
        raise GitHubFeedbackConfigurationError(
            "GitHub returned a timezone-naive installation token expiry"
        )
    return parsed.astimezone(UTC)
