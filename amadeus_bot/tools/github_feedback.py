from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol
from urllib.parse import quote

import httpx

_MAX_GROUNDED_FILE_BYTES = 128_000
_MAX_AFFECTED_FILES = 5


class GitHubFeedbackError(RuntimeError):
    """Base error for the bounded public GitHub feedback capability."""


class GitHubFeedbackConfigurationError(GitHubFeedbackError):
    """Raised when the capability target or credentials are invalid."""


class GitHubFeedbackGuardError(GitHubFeedbackError):
    """Raised when generated public text fails the outbound guard."""


class GitHubFeedbackConfirmationRequired(GitHubFeedbackError):
    """Raised when a persistent GitHub action is attempted without confirmation."""


class GitHubFeedbackStaleProposal(GitHubFeedbackError):
    """Raised when the public repository changed after a proposal was grounded."""


class GitHubFeedbackAPIError(GitHubFeedbackError):
    """Raised when GitHub rejects or fails a capability request."""

    def __init__(self, *, status_code: int, operation: str) -> None:
        super().__init__(f"GitHub {operation} failed with HTTP {status_code}")
        self.status_code = status_code
        self.operation = operation


class GitHubAccessTokenProvider(Protocol):
    """Backend-only token source. Character code never receives the returned token."""

    async def get_token(self) -> str: ...


@dataclass(frozen=True, slots=True)
class GitHubRepositoryState:
    repository: str
    branch: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class GitHubPublicFile:
    path: str
    sha: str
    size: int
    content: str
    public_head: str


@dataclass(frozen=True, slots=True)
class GitHubIssueSummary:
    number: int
    title: str
    state: str
    html_url: str


@dataclass(frozen=True, slots=True)
class GitHubIssueProposal:
    proposal_id: str
    title: str
    body: str
    public_head: str
    affected_files: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    fingerprint: str | None = None

    @classmethod
    def build(
        cls,
        *,
        title: str,
        body: str,
        public_head: str,
        affected_files: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        fingerprint: str | None = None,
    ) -> GitHubIssueProposal:
        normalized_title = title.strip()
        normalized_body = body.strip()
        normalized_head = public_head.strip()
        normalized_files = tuple(path.strip() for path in affected_files if path.strip())
        if not normalized_title:
            raise ValueError("issue title must not be empty")
        if not normalized_body:
            raise ValueError("issue body must not be empty")
        if not normalized_head:
            raise ValueError("public_head must not be empty")
        stable_fingerprint = fingerprint or _fingerprint(
            normalized_title,
            "\n".join(sorted(normalized_files)),
        )
        proposal_id = _fingerprint(normalized_head, stable_fingerprint, normalized_body)[:20]
        return cls(
            proposal_id=proposal_id,
            title=normalized_title,
            body=normalized_body,
            public_head=normalized_head,
            affected_files=normalized_files,
            labels=tuple(label.strip() for label in labels if label.strip()),
            fingerprint=stable_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class GitHubCommentProposal:
    proposal_id: str
    issue_number: int
    body: str
    public_head: str

    @classmethod
    def build(
        cls,
        *,
        issue_number: int,
        body: str,
        public_head: str,
    ) -> GitHubCommentProposal:
        normalized_body = body.strip()
        normalized_head = public_head.strip()
        if issue_number <= 0:
            raise ValueError("issue_number must be positive")
        if not normalized_body:
            raise ValueError("comment body must not be empty")
        if not normalized_head:
            raise ValueError("public_head must not be empty")
        proposal_id = _fingerprint(str(issue_number), normalized_head, normalized_body)[:20]
        return cls(
            proposal_id=proposal_id,
            issue_number=issue_number,
            body=normalized_body,
            public_head=normalized_head,
        )


@dataclass(frozen=True, slots=True)
class PreparedGitHubIssue:
    proposal: GitHubIssueProposal
    duplicate_candidates: tuple[GitHubIssueSummary, ...]
    grounded_files: tuple[GitHubPublicFile, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedGitHubComment:
    proposal: GitHubCommentProposal


class PublicOutboundGuard:
    """Deterministic defense-in-depth checks for generated public GitHub text."""

    _DEFAULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        (
            "private_key",
            re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
        ),
        (
            "github_token",
            re.compile(r"\b(?:github_[p]at_|gh[pousr]_[A-Za-z0-9_]+)", re.IGNORECASE),
        ),
        (
            "bearer_token",
            re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
        ),
        (
            "env_secret",
            re.compile(
                r"\b(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)\s*=\s*[^\s]{8,}",
                re.IGNORECASE,
            ),
        ),
        (
            "unix_home_path",
            re.compile(r"/(?:home|Users)/[^\s/]+/(?:[^\s]+)"),
        ),
        (
            "windows_user_path",
            re.compile(r"\b[A-Za-z]:\\Users\\[^\\\s]+\\[^\s]+", re.IGNORECASE),
        ),
        (
            "private_ipv4",
            re.compile(
                r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
                r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
            ),
        ),
    )

    def __init__(self, *, forbidden_fragments: tuple[str, ...] = ()) -> None:
        self._forbidden_fragments = tuple(
            fragment.casefold() for fragment in forbidden_fragments if fragment.strip()
        )

    def validate(self, *parts: str) -> None:
        text = "\n".join(parts)
        for label, pattern in self._DEFAULT_PATTERNS:
            if pattern.search(text):
                raise GitHubFeedbackGuardError(f"public outbound guard blocked: {label}")
        folded = text.casefold()
        for fragment in self._forbidden_fragments:
            if fragment in folded:
                raise GitHubFeedbackGuardError(
                    "public outbound guard blocked: operator_forbidden_fragment"
                )


class GitHubFeedbackClient:
    """Repository-pinned GitHub Issues client with explicit side-effect gates."""

    def __init__(
        self,
        *,
        repository: str,
        branch: str,
        token_provider: GitHubAccessTokenProvider,
        allowed_labels: frozenset[str] = frozenset(),
        outbound_guard: PublicOutboundGuard | None = None,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_repository = repository.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", normalized_repository):
            raise GitHubFeedbackConfigurationError("repository must use owner/name form")
        normalized_branch = branch.strip()
        if not normalized_branch:
            raise GitHubFeedbackConfigurationError("branch must not be empty")
        self._repository = normalized_repository
        self._branch = normalized_branch
        self._token_provider = token_provider
        self._allowed_labels = allowed_labels
        self._guard = outbound_guard or PublicOutboundGuard()
        self._client = httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
        )

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def branch(self) -> str:
        return self._branch

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_public_repo_state(self) -> GitHubRepositoryState:
        response = await self._request(
            "GET",
            f"/repos/{self._repository}/commits/{self._branch}",
            operation="read public HEAD",
        )
        payload = response.json()
        head_sha = payload.get("sha")
        if not isinstance(head_sha, str) or not head_sha.strip():
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse public HEAD",
            )
        return GitHubRepositoryState(
            repository=self._repository,
            branch=self._branch,
            head_sha=head_sha,
        )

    async def read_public_file(
        self,
        path: str,
        *,
        public_head: str,
    ) -> GitHubPublicFile:
        normalized_path = _normalize_public_path(path)
        normalized_head = public_head.strip()
        if not normalized_head:
            raise ValueError("public_head must not be empty")
        encoded_path = quote(normalized_path, safe="/")
        response = await self._request(
            "GET",
            f"/repos/{self._repository}/contents/{encoded_path}",
            operation="read public file",
            params={"ref": normalized_head},
        )
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse public file",
            )
        sha = payload.get("sha")
        size = payload.get("size")
        encoding = payload.get("encoding")
        encoded_content = payload.get("content")
        if (
            not isinstance(sha, str)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(encoding, str)
            or not isinstance(encoded_content, str)
        ):
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse public file",
            )
        if size > _MAX_GROUNDED_FILE_BYTES:
            raise GitHubFeedbackConfigurationError("public grounding file exceeds size limit")
        if encoding.casefold() != "base64":
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse public file encoding",
            )
        try:
            decoded = base64.b64decode(encoded_content, validate=False)
            content = decoded.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="decode public file",
            ) from exc
        if len(decoded) > _MAX_GROUNDED_FILE_BYTES:
            raise GitHubFeedbackConfigurationError("public grounding file exceeds size limit")
        return GitHubPublicFile(
            path=normalized_path,
            sha=sha,
            size=size,
            content=content,
            public_head=normalized_head,
        )

    async def search_issues(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> tuple[GitHubIssueSummary, ...]:
        normalized_query = " ".join(query.split()).strip()
        if not normalized_query:
            raise ValueError("issue search query must not be empty")
        if limit <= 0 or limit > 20:
            raise ValueError("issue search limit must be between 1 and 20")
        response = await self._request(
            "GET",
            "/search/issues",
            operation="search issues",
            params={
                "q": f"repo:{self._repository} is:issue {normalized_query}",
                "per_page": str(limit),
            },
        )
        payload = response.json()
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse issue search",
            )
        summaries: list[GitHubIssueSummary] = []
        for raw in raw_items:
            summary = _parse_issue_summary(raw)
            if summary is not None:
                summaries.append(summary)
        return tuple(summaries)

    async def prepare_issue(self, proposal: GitHubIssueProposal) -> PreparedGitHubIssue:
        self._validate_issue_proposal(proposal)
        state = await self.get_public_repo_state()
        self._require_fresh_head(proposal.public_head, state)
        grounded_files = tuple(
            [
                await self.read_public_file(path, public_head=proposal.public_head)
                for path in proposal.affected_files
            ]
        )
        duplicates = await self.search_issues(proposal.title)
        return PreparedGitHubIssue(
            proposal=proposal,
            duplicate_candidates=duplicates,
            grounded_files=grounded_files,
        )

    async def create_issue(
        self,
        prepared: PreparedGitHubIssue,
        *,
        confirmed: bool,
    ) -> GitHubIssueSummary:
        if not confirmed:
            raise GitHubFeedbackConfirmationRequired(
                "issue creation requires explicit confirmation"
            )
        proposal = prepared.proposal
        self._validate_issue_proposal(proposal)
        state = await self.get_public_repo_state()
        self._require_fresh_head(proposal.public_head, state)
        response = await self._request(
            "POST",
            f"/repos/{self._repository}/issues",
            operation="create issue",
            json={
                "title": proposal.title,
                "body": proposal.body,
                "labels": list(proposal.labels),
            },
        )
        summary = _parse_issue_summary(response.json())
        if summary is None:
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse created issue",
            )
        return summary

    async def prepare_comment(self, proposal: GitHubCommentProposal) -> PreparedGitHubComment:
        self._guard.validate(proposal.body)
        state = await self.get_public_repo_state()
        self._require_fresh_head(proposal.public_head, state)
        return PreparedGitHubComment(proposal=proposal)

    async def comment_issue(
        self,
        prepared: PreparedGitHubComment,
        *,
        confirmed: bool,
    ) -> str:
        if not confirmed:
            raise GitHubFeedbackConfirmationRequired("issue comment requires explicit confirmation")
        proposal = prepared.proposal
        self._guard.validate(proposal.body)
        state = await self.get_public_repo_state()
        self._require_fresh_head(proposal.public_head, state)
        response = await self._request(
            "POST",
            f"/repos/{self._repository}/issues/{proposal.issue_number}/comments",
            operation="comment issue",
            json={"body": proposal.body},
        )
        payload = response.json()
        html_url = payload.get("html_url")
        if not isinstance(html_url, str) or not html_url:
            raise GitHubFeedbackAPIError(
                status_code=response.status_code,
                operation="parse issue comment",
            )
        return html_url

    def _validate_issue_proposal(self, proposal: GitHubIssueProposal) -> None:
        self._guard.validate(proposal.title, proposal.body, *proposal.affected_files)
        if len(proposal.affected_files) > _MAX_AFFECTED_FILES:
            raise GitHubFeedbackGuardError("issue proposal contains too many affected files")
        if len(set(proposal.affected_files)) != len(proposal.affected_files):
            raise GitHubFeedbackGuardError("issue proposal contains duplicate affected files")
        for path in proposal.affected_files:
            _normalize_public_path(path)
        disallowed = set(proposal.labels) - self._allowed_labels
        if disallowed:
            raise GitHubFeedbackGuardError("issue proposal contains non-allow-listed labels")

    def _require_fresh_head(
        self,
        proposal_head: str,
        state: GitHubRepositoryState,
    ) -> None:
        if proposal_head != state.head_sha:
            raise GitHubFeedbackStaleProposal(
                "public repository HEAD changed after proposal grounding"
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        token = (await self._token_provider.get_token()).strip()
        if not token:
            raise GitHubFeedbackConfigurationError("GitHub access token provider returned no token")
        response = await self._client.request(
            method,
            path,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise GitHubFeedbackAPIError(status_code=response.status_code, operation=operation)
        return response


def _normalize_public_path(path: str) -> str:
    normalized = path.strip()
    if not normalized:
        raise GitHubFeedbackGuardError("public file path must not be empty")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise GitHubFeedbackGuardError("public file path must be repository-relative")
    return candidate.as_posix()


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest


def _parse_issue_summary(raw: object) -> GitHubIssueSummary | None:
    if not isinstance(raw, dict):
        return None
    number = raw.get("number")
    title = raw.get("title")
    state = raw.get("state")
    html_url = raw.get("html_url")
    if (
        not isinstance(number, int)
        or not isinstance(title, str)
        or not isinstance(state, str)
        or not isinstance(html_url, str)
    ):
        return None
    return GitHubIssueSummary(number=number, title=title, state=state, html_url=html_url)
