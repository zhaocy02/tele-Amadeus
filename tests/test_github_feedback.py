from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass

import httpx
import pytest

from amadeus_bot.tools.github_feedback import (
    GitHubCommentProposal,
    GitHubFeedbackAPIError,
    GitHubFeedbackClient,
    GitHubFeedbackConfirmationRequired,
    GitHubFeedbackGuardError,
    GitHubFeedbackStaleProposal,
    GitHubIssueProposal,
    PublicOutboundGuard,
)


@dataclass(slots=True)
class StaticTokenProvider:
    token: str = "test-installation-token"

    async def get_token(self) -> str:
        return self.token


class RecordingGitHub:
    def __init__(self, *, head: str = "public-head-1") -> None:
        self.head = head
        self.requests: list[httpx.Request] = []
        self.public_files = {
            "amadeus_bot/memory/example.py": (
                "def correction_path() -> str:\n    return 'authoritative'\n"
            )
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": self.head})
        prefix = "/repos/example/public/contents/"
        if request.method == "GET" and path.startswith(prefix):
            relative = path.removeprefix(prefix)
            content = self.public_files.get(relative)
            if content is None:
                return httpx.Response(404, json={"message": "not found"})
            encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "sha": f"blob-{relative}",
                    "size": len(content.encode("utf-8")),
                    "encoding": "base64",
                    "content": encoded,
                },
            )
        if request.method == "GET" and path == "/search/issues":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "number": 12,
                            "title": "Existing memory correction issue",
                            "state": "open",
                            "html_url": "https://github.com/example/public/issues/12",
                        }
                    ]
                },
            )
        if request.method == "POST" and path.endswith("/issues"):
            payload = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "number": 13,
                    "title": payload["title"],
                    "state": "open",
                    "html_url": "https://github.com/example/public/issues/13",
                },
            )
        if request.method == "POST" and path.endswith("/issues/13/comments"):
            return httpx.Response(
                201,
                json={"html_url": "https://github.com/example/public/issues/13#issuecomment-1"},
            )
        return httpx.Response(404, json={"message": "not found"})


def proposal(*, head: str = "public-head-1") -> GitHubIssueProposal:
    return GitHubIssueProposal.build(
        title="Memory correction can revive a stale assumption",
        body=(
            "Observed on the current public snapshot. "
            "The correction path should stay authoritative."
        ),
        public_head=head,
        affected_files=("amadeus_bot/memory/example.py",),
        labels=("from-amadeus", "memory"),
    )


def test_prepare_issue_scopes_duplicate_search_to_pinned_repository() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            allowed_labels=frozenset({"from-amadeus", "memory"}),
            transport=github.transport(),
        )
        try:
            prepared = await client.prepare_issue(proposal())
        finally:
            await client.aclose()

        assert prepared.duplicate_candidates[0].number == 12
        assert prepared.grounded_files[0].path == "amadeus_bot/memory/example.py"
        assert "authoritative" in prepared.grounded_files[0].content
        grounding_request = next(
            request for request in github.requests if "/contents/" in request.url.path
        )
        assert grounding_request.url.params["ref"] == "public-head-1"
        search_request = next(
            request for request in github.requests if request.url.path == "/search/issues"
        )
        assert "repo:example/public" in search_request.url.params["q"]
        assert "is:issue" in search_request.url.params["q"]

    asyncio.run(scenario())


def test_missing_public_file_blocks_prepare_before_duplicate_search() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        github.public_files.clear()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            allowed_labels=frozenset({"from-amadeus", "memory"}),
            transport=github.transport(),
        )
        try:
            with pytest.raises(GitHubFeedbackAPIError, match="read public file"):
                await client.prepare_issue(proposal())
        finally:
            await client.aclose()

        assert not any(request.url.path == "/search/issues" for request in github.requests)
        assert not any(request.method == "POST" for request in github.requests)

    asyncio.run(scenario())


def test_create_issue_requires_explicit_confirmation_and_makes_no_post() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            allowed_labels=frozenset({"from-amadeus", "memory"}),
            transport=github.transport(),
        )
        try:
            prepared = await client.prepare_issue(proposal())
            with pytest.raises(GitHubFeedbackConfirmationRequired):
                await client.create_issue(prepared, confirmed=False)
        finally:
            await client.aclose()

        assert not any(request.method == "POST" for request in github.requests)

    asyncio.run(scenario())


def test_create_issue_rechecks_public_head_before_write() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            allowed_labels=frozenset({"from-amadeus", "memory"}),
            transport=github.transport(),
        )
        try:
            prepared = await client.prepare_issue(proposal())
            github.head = "public-head-2"
            with pytest.raises(GitHubFeedbackStaleProposal):
                await client.create_issue(prepared, confirmed=True)
        finally:
            await client.aclose()

        assert not any(request.method == "POST" for request in github.requests)

    asyncio.run(scenario())


def test_create_issue_writes_exact_prepared_proposal_after_confirmation() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            allowed_labels=frozenset({"from-amadeus", "memory"}),
            transport=github.transport(),
        )
        issue = proposal()
        try:
            prepared = await client.prepare_issue(issue)
            created = await client.create_issue(prepared, confirmed=True)
        finally:
            await client.aclose()

        assert created.number == 13
        post = next(request for request in github.requests if request.method == "POST")
        payload = json.loads(post.content)
        assert payload == {
            "title": issue.title,
            "body": issue.body,
            "labels": ["from-amadeus", "memory"],
        }
        assert post.url.path == "/repos/example/public/issues"

    asyncio.run(scenario())


def test_comment_requires_confirmation_and_stale_head_check() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            transport=github.transport(),
        )
        comment = GitHubCommentProposal.build(
            issue_number=13,
            body="I re-checked the public code and the relevant path changed.",
            public_head="public-head-1",
        )
        try:
            prepared = await client.prepare_comment(comment)
            with pytest.raises(GitHubFeedbackConfirmationRequired):
                await client.comment_issue(prepared, confirmed=False)
            github.head = "public-head-2"
            with pytest.raises(GitHubFeedbackStaleProposal):
                await client.comment_issue(prepared, confirmed=True)
        finally:
            await client.aclose()

        assert not any(request.method == "POST" for request in github.requests)

    asyncio.run(scenario())


def test_outbound_guard_blocks_secret_and_private_context_patterns() -> None:
    guard = PublicOutboundGuard(forbidden_fragments=("private-only-marker",))
    short_token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz"
    private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"

    with pytest.raises(GitHubFeedbackGuardError, match="private_key"):
        guard.validate(private_key_marker)
    with pytest.raises(GitHubFeedbackGuardError, match="github_token"):
        guard.validate(f"token={short_token}")
    with pytest.raises(GitHubFeedbackGuardError, match="unix_home_path"):
        guard.validate("see /home/operator/project/runtime.sqlite")
    with pytest.raises(GitHubFeedbackGuardError, match="private_ipv4"):
        guard.validate("service at 192.0.2.10")
    with pytest.raises(GitHubFeedbackGuardError, match="operator_forbidden_fragment"):
        guard.validate("This includes PRIVATE-ONLY-MARKER from hidden context")


def test_issue_labels_are_operator_allow_listed() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            allowed_labels=frozenset({"from-amadeus"}),
            transport=github.transport(),
        )
        issue = GitHubIssueProposal.build(
            title="A bounded observation",
            body="This is grounded in the public snapshot.",
            public_head="public-head-1",
            labels=("from-amadeus", "admin"),
        )

        try:
            with pytest.raises(GitHubFeedbackGuardError, match="non-allow-listed"):
                await client.prepare_issue(issue)
        finally:
            await client.aclose()

        assert github.requests == []

    asyncio.run(scenario())


def test_affected_file_path_must_stay_repository_relative() -> None:
    async def scenario() -> None:
        github = RecordingGitHub()
        client = GitHubFeedbackClient(
            repository="example/public",
            branch="main",
            token_provider=StaticTokenProvider(),
            transport=github.transport(),
        )
        issue = GitHubIssueProposal.build(
            title="Bad path",
            body="This must not escape the repository.",
            public_head="public-head-1",
            affected_files=("../private/file.py",),
        )
        try:
            with pytest.raises(GitHubFeedbackGuardError, match="repository-relative"):
                await client.prepare_issue(issue)
        finally:
            await client.aclose()

        assert github.requests == []

    asyncio.run(scenario())
