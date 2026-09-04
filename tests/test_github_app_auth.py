from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from amadeus_bot.tools.github_app_auth import (
    GitHubAppInstallationConfig,
    GitHubAppInstallationTokenProvider,
)
from amadeus_bot.tools.github_feedback import GitHubFeedbackConfigurationError


class RecordingTokenEndpoint:
    def __init__(
        self,
        *,
        permissions: dict[str, str] | None = None,
        repository: str = "example/public",
    ) -> None:
        self.permissions = permissions or {
            "contents": "read",
            "issues": "write",
            "metadata": "read",
        }
        self.repository = repository
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            201,
            json={
                "token": "installation-token-value",
                "expires_at": "2026-09-04T16:00:00Z",
                "permissions": self.permissions,
                "repositories": [{"full_name": self.repository}],
            },
        )


def _write_private_key(path: Path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encoded = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(encoded)


def _config(tmp_path: Path) -> GitHubAppInstallationConfig:
    key_path = tmp_path / "app-key.pem"
    _write_private_key(key_path)
    return GitHubAppInstallationConfig(
        client_id="Iv1.example-client",
        installation_id=12345,
        private_key_path=key_path,
        repository="example/public",
    )


def test_token_request_is_repository_and_permission_scoped(tmp_path: Path) -> None:
    async def scenario() -> None:
        endpoint = RecordingTokenEndpoint()
        now = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
        provider = GitHubAppInstallationTokenProvider(
            _config(tmp_path),
            clock=lambda: now,
            transport=endpoint.transport(),
        )
        try:
            token = await provider.get_token()
        finally:
            await provider.aclose()

        assert token == "installation-token-value"
        assert len(endpoint.requests) == 1
        request = endpoint.requests[0]
        assert request.url.path == "/app/installations/12345/access_tokens"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        payload = json.loads(request.content)
        assert payload == {
            "repositories": ["public"],
            "permissions": {
                "contents": "read",
                "issues": "write",
                "metadata": "read",
            },
        }

        authorization = request.headers["Authorization"]
        assert authorization.startswith("Bearer ")
        app_jwt = authorization.removeprefix("Bearer ")
        header = jwt.get_unverified_header(app_jwt)
        claims = jwt.decode(app_jwt, options={"verify_signature": False})
        assert header["alg"] == "RS256"
        assert claims["iss"] == "Iv1.example-client"
        assert claims["iat"] == int(now.timestamp()) - 60
        assert claims["exp"] == int(now.timestamp()) + 9 * 60

    asyncio.run(scenario())


def test_installation_token_is_cached_until_refresh_window(tmp_path: Path) -> None:
    async def scenario() -> None:
        endpoint = RecordingTokenEndpoint()
        now = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
        provider = GitHubAppInstallationTokenProvider(
            _config(tmp_path),
            clock=lambda: now,
            transport=endpoint.transport(),
        )
        try:
            first = await provider.get_token()
            second = await provider.get_token()
        finally:
            await provider.aclose()

        assert first == second == "installation-token-value"
        assert len(endpoint.requests) == 1

    asyncio.run(scenario())


def test_unexpected_write_permission_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        endpoint = RecordingTokenEndpoint(
            permissions={
                "contents": "write",
                "issues": "write",
                "metadata": "read",
            }
        )
        now = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
        provider = GitHubAppInstallationTokenProvider(
            _config(tmp_path),
            clock=lambda: now,
            transport=endpoint.transport(),
        )
        try:
            with pytest.raises(GitHubFeedbackConfigurationError, match="permission scope"):
                await provider.get_token()
        finally:
            await provider.aclose()

    asyncio.run(scenario())


def test_repository_scope_mismatch_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        endpoint = RecordingTokenEndpoint(repository="example/other")
        now = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
        provider = GitHubAppInstallationTokenProvider(
            _config(tmp_path),
            clock=lambda: now,
            transport=endpoint.transport(),
        )
        try:
            with pytest.raises(GitHubFeedbackConfigurationError, match="not pinned"):
                await provider.get_token()
        finally:
            await provider.aclose()

    asyncio.run(scenario())


def test_private_key_read_failure_does_not_echo_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = GitHubAppInstallationConfig(
            client_id="Iv1.example-client",
            installation_id=12345,
            private_key_path=tmp_path / "missing.pem",
            repository="example/public",
        )
        provider = GitHubAppInstallationTokenProvider(config)
        try:
            with pytest.raises(GitHubFeedbackConfigurationError) as captured:
                await provider.get_token()
        finally:
            await provider.aclose()

        assert "missing.pem" not in str(captured.value)

    asyncio.run(scenario())
