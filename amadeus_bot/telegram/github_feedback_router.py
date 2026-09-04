from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from amadeus_bot.tools import (
    GitHubFeedbackAPIError,
    GitHubFeedbackConfigurationError,
    GitHubFeedbackGuardError,
    GitHubFeedbackStaleProposal,
    GitHubIssueProposal,
    GitHubIssueSummary,
    GitHubRepositoryState,
    PreparedGitHubIssue,
)

from .adapter import TelegramGateway

_MAX_TITLE_CHARS = 180
_MAX_BODY_CHARS = 2400
_MAX_AFFECTED_FILES = 5
_DEFAULT_TTL = timedelta(minutes=15)


class GitHubFeedbackActions(Protocol):
    @property
    def repository(self) -> str: ...

    @property
    def branch(self) -> str: ...

    async def get_public_repo_state(self) -> GitHubRepositoryState: ...

    async def prepare_issue(self, proposal: GitHubIssueProposal) -> PreparedGitHubIssue: ...

    async def create_issue(
        self,
        prepared: PreparedGitHubIssue,
        *,
        confirmed: bool,
    ) -> GitHubIssueSummary: ...


@dataclass(frozen=True, slots=True)
class _PendingIssue:
    prepared: PreparedGitHubIssue
    expires_at: datetime


class GitHubFeedbackCommandRouter:
    """Explicit Stage-1 proposal/confirm control for persistent public GitHub writes."""

    def __init__(
        self,
        *,
        gateway: TelegramGateway,
        feedback: GitHubFeedbackActions,
        proposal_ttl: timedelta = _DEFAULT_TTL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if proposal_ttl <= timedelta(0) or proposal_ttl > timedelta(hours=1):
            raise ValueError("proposal_ttl must be between 0 and 1 hour")
        self._gateway = gateway
        self._feedback = feedback
        self._proposal_ttl = proposal_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending: dict[int, _PendingIssue] = {}

    async def handle(self, chat_id: int, argument: str) -> None:
        command, payload = _split_subcommand(argument)
        if command in {"", "help", "status"}:
            await self._send(chat_id, self._status_text(chat_id))
            return
        if command == "propose":
            await self._propose(chat_id, payload)
            return
        if command == "pending":
            pending = self._current_pending(chat_id)
            await self._send(
                chat_id,
                self._render_preview(pending.prepared)
                if pending is not None
                else "当前没有待确认的 GitHub feedback proposal。",
            )
            return
        if command == "confirm":
            await self._confirm(chat_id, payload)
            return
        if command == "cancel":
            await self._cancel(chat_id, payload)
            return
        await self._send(chat_id, self._usage_text())

    async def _propose(self, chat_id: int, payload: str) -> None:
        try:
            title, body, affected_files = _parse_proposal_payload(payload)
            state = await self._feedback.get_public_repo_state()
            provenance = _render_provenance(state, affected_files)
            proposal = GitHubIssueProposal.build(
                title=title,
                body=f"{body}\n\n{provenance}",
                public_head=state.head_sha,
                affected_files=affected_files,
            )
            prepared = await self._feedback.prepare_issue(proposal)
        except ValueError as exc:
            await self._send(chat_id, f"proposal 参数无效：{exc}")
            return
        except GitHubFeedbackGuardError:
            await self._send(
                chat_id,
                "proposal 被 Public Outbound Guard 拒绝，未产生任何外部写入。",
            )
            return
        except (GitHubFeedbackAPIError, GitHubFeedbackConfigurationError):
            await self._send(chat_id, "GitHub feedback 当前不可用；没有创建 Issue。")
            return

        self._pending[chat_id] = _PendingIssue(
            prepared=prepared,
            expires_at=self._utc_now() + self._proposal_ttl,
        )
        await self._send(chat_id, self._render_preview(prepared))

    async def _confirm(self, chat_id: int, payload: str) -> None:
        pending = self._current_pending(chat_id)
        if pending is None:
            await self._send(chat_id, "没有可确认的 proposal，或 proposal 已过期。")
            return
        proposal_id = payload.strip()
        expected = pending.prepared.proposal.proposal_id
        if proposal_id != expected:
            await self._send(chat_id, f"proposal id 不匹配。当前待确认 id={expected}")
            return

        try:
            created = await self._feedback.create_issue(pending.prepared, confirmed=True)
        except GitHubFeedbackStaleProposal:
            self._pending.pop(chat_id, None)
            await self._send(
                chat_id,
                "public mirror HEAD 已变化；旧 proposal 已失效。请重新生成后再确认。",
            )
            return
        except GitHubFeedbackGuardError:
            self._pending.pop(chat_id, None)
            await self._send(chat_id, "确认前安全检查失败；proposal 已丢弃，没有创建 Issue。")
            return
        except (GitHubFeedbackAPIError, GitHubFeedbackConfigurationError):
            await self._send(chat_id, "GitHub 写入失败；proposal 仍保留，可稍后再次确认。")
            return

        self._pending.pop(chat_id, None)
        await self._send(chat_id, f"已创建 public Issue #{created.number}\n{created.html_url}")

    async def _cancel(self, chat_id: int, payload: str) -> None:
        pending = self._current_pending(chat_id)
        if pending is None:
            await self._send(chat_id, "当前没有待取消的 proposal。")
            return
        proposal_id = payload.strip()
        expected = pending.prepared.proposal.proposal_id
        if proposal_id and proposal_id != expected:
            await self._send(chat_id, f"proposal id 不匹配。当前待确认 id={expected}")
            return
        self._pending.pop(chat_id, None)
        await self._send(chat_id, f"已取消 proposal {expected}；没有产生 GitHub 写入。")

    def _current_pending(self, chat_id: int) -> _PendingIssue | None:
        pending = self._pending.get(chat_id)
        if pending is None:
            return None
        if self._utc_now() >= pending.expires_at:
            self._pending.pop(chat_id, None)
            return None
        return pending

    def _status_text(self, chat_id: int) -> str:
        pending = self._current_pending(chat_id)
        pending_text = "none" if pending is None else pending.prepared.proposal.proposal_id
        return "\n".join(
            (
                "Public GitHub Feedback (Stage 1)",
                f"repository={self._feedback.repository}",
                f"branch={self._feedback.branch}",
                "write_mode=explicit_confirmation_only",
                f"pending={pending_text}",
                self._usage_text(),
            )
        )

    @staticmethod
    def _usage_text() -> str:
        return (
            "用法：\n"
            "/github propose <标题>\n<正文>\n"
            "可选最后一行：files: path1, path2\n"
            "/github pending\n"
            "/github confirm <proposal_id>\n"
            "/github cancel [proposal_id]"
        )

    @staticmethod
    def _render_preview(prepared: PreparedGitHubIssue) -> str:
        proposal = prepared.proposal
        lines = [
            "GitHub feedback proposal（尚未写入）",
            f"id={proposal.proposal_id}",
            f"public_head={proposal.public_head}",
            f"files={', '.join(proposal.affected_files) if proposal.affected_files else 'none'}",
            f"duplicate_candidates={len(prepared.duplicate_candidates)}",
        ]
        for item in prepared.duplicate_candidates[:5]:
            lines.append(f"- #{item.number} [{item.state}] {item.title}")
        lines.extend(
            (
                "",
                f"Title:\n{proposal.title}",
                "",
                f"Body:\n{proposal.body}",
                "",
                f"确认：/github confirm {proposal.proposal_id}",
                f"取消：/github cancel {proposal.proposal_id}",
            )
        )
        return "\n".join(lines)

    async def _send(self, chat_id: int, text: str) -> None:
        await self._gateway.send_message(chat_id, text)

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("GitHub feedback router clock must be timezone-aware")
        return now.astimezone(UTC)


def _split_subcommand(argument: str) -> tuple[str, str]:
    stripped = argument.strip()
    if not stripped:
        return "", ""
    first, separator, rest = stripped.partition(" ")
    if not separator:
        return first.casefold(), ""
    return first.casefold(), rest.lstrip()


def _parse_proposal_payload(payload: str) -> tuple[str, str, tuple[str, ...]]:
    lines = payload.splitlines()
    if len(lines) < 2:
        raise ValueError("propose 需要第一行标题和至少一行正文")
    title = lines[0].strip()
    body_lines = lines[1:]
    affected_files: tuple[str, ...] = ()
    if body_lines and body_lines[-1].strip().casefold().startswith("files:"):
        raw_files = body_lines.pop().split(":", maxsplit=1)[1]
        affected_files = tuple(item.strip() for item in raw_files.split(",") if item.strip())
    body = "\n".join(body_lines).strip()
    if not title:
        raise ValueError("标题不能为空")
    if not body:
        raise ValueError("正文不能为空")
    if len(title) > _MAX_TITLE_CHARS:
        raise ValueError(f"标题不能超过 {_MAX_TITLE_CHARS} 个字符")
    if len(body) > _MAX_BODY_CHARS:
        raise ValueError(f"正文不能超过 {_MAX_BODY_CHARS} 个字符")
    if len(affected_files) > _MAX_AFFECTED_FILES:
        raise ValueError(f"affected files 最多 {_MAX_AFFECTED_FILES} 个")
    return title, body, affected_files


def _render_provenance(
    state: GitHubRepositoryState,
    affected_files: tuple[str, ...],
) -> str:
    lines = [
        "---",
        "Public mirror provenance",
        f"- repository: `{state.repository}`",
        f"- branch: `{state.branch}`",
        f"- head: `{state.head_sha}`",
    ]
    if affected_files:
        lines.append("- affected files:")
        lines.extend(f"  - `{path}`" for path in affected_files)
    lines.append("- publication path: Amadeus bounded GitHub feedback capability")
    return "\n".join(lines)
