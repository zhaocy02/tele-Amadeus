from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from amadeus_bot.telegram.github_feedback_router import GitHubFeedbackCommandRouter
from amadeus_bot.tools import (
    GitHubFeedbackStaleProposal,
    GitHubIssueProposal,
    GitHubIssueSummary,
    GitHubRepositoryState,
    PreparedGitHubIssue,
)


class FakeGateway:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> int:
        del chat_id
        self.messages.append(text)
        return len(self.messages)


class FakeFeedback:
    repository = "example/public"
    branch = "main"

    def __init__(self) -> None:
        self.prepared: list[GitHubIssueProposal] = []
        self.created: list[PreparedGitHubIssue] = []
        self.raise_stale = False

    async def get_public_repo_state(self) -> GitHubRepositoryState:
        return GitHubRepositoryState(
            repository=self.repository,
            branch=self.branch,
            head_sha="public-head-1",
        )

    async def prepare_issue(self, proposal: GitHubIssueProposal) -> PreparedGitHubIssue:
        self.prepared.append(proposal)
        return PreparedGitHubIssue(
            proposal=proposal,
            duplicate_candidates=(
                GitHubIssueSummary(
                    number=7,
                    title="Existing related issue",
                    state="open",
                    html_url="https://github.com/example/public/issues/7",
                ),
            ),
        )

    async def create_issue(
        self,
        prepared: PreparedGitHubIssue,
        *,
        confirmed: bool,
    ) -> GitHubIssueSummary:
        assert confirmed is True
        if self.raise_stale:
            raise GitHubFeedbackStaleProposal("stale")
        self.created.append(prepared)
        return GitHubIssueSummary(
            number=8,
            title=prepared.proposal.title,
            state="open",
            html_url="https://github.com/example/public/issues/8",
        )


def test_propose_is_side_effect_free_until_exact_confirmation() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        feedback = FakeFeedback()
        router = GitHubFeedbackCommandRouter(gateway=gateway, feedback=feedback)

        await router.handle(
            42,
            "propose Memory correction ordering\n"
            "The correction path should remain authoritative.\n"
            "files: amadeus_bot/memory/example.py",
        )

        assert feedback.created == []
        assert len(feedback.prepared) == 1
        proposal = feedback.prepared[0]
        assert proposal.affected_files == ("amadeus_bot/memory/example.py",)
        assert "Public mirror provenance" in proposal.body
        assert "public-head-1" in proposal.body
        assert "duplicate_candidates=1" in gateway.messages[-1]
        assert f"/github confirm {proposal.proposal_id}" in gateway.messages[-1]

        await router.handle(42, f"confirm {proposal.proposal_id}")

        assert len(feedback.created) == 1
        assert feedback.created[0].proposal == proposal
        assert "已创建 public Issue #8" in gateway.messages[-1]

    asyncio.run(scenario())


def test_wrong_confirmation_id_cannot_write() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        feedback = FakeFeedback()
        router = GitHubFeedbackCommandRouter(gateway=gateway, feedback=feedback)

        await router.handle(42, "propose A bounded idea\nThis should stay bounded.")
        await router.handle(42, "confirm wrong-id")

        assert feedback.created == []
        assert "proposal id 不匹配" in gateway.messages[-1]

    asyncio.run(scenario())


def test_stale_head_drops_pending_proposal() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        feedback = FakeFeedback()
        feedback.raise_stale = True
        router = GitHubFeedbackCommandRouter(gateway=gateway, feedback=feedback)

        await router.handle(42, "propose A bounded idea\nThis should stay bounded.")
        proposal_id = feedback.prepared[0].proposal_id
        await router.handle(42, f"confirm {proposal_id}")
        await router.handle(42, "pending")

        assert feedback.created == []
        assert "当前没有待确认" in gateway.messages[-1]

    asyncio.run(scenario())


def test_expired_proposal_cannot_write() -> None:
    async def scenario() -> None:
        gateway = FakeGateway()
        feedback = FakeFeedback()
        now = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
        clock_value = [now]
        router = GitHubFeedbackCommandRouter(
            gateway=gateway,
            feedback=feedback,
            proposal_ttl=timedelta(minutes=15),
            clock=lambda: clock_value[0],
        )

        await router.handle(42, "propose A bounded idea\nThis should stay bounded.")
        proposal_id = feedback.prepared[0].proposal_id
        clock_value[0] = now + timedelta(minutes=16)
        await router.handle(42, f"confirm {proposal_id}")

        assert feedback.created == []
        assert "已过期" in gateway.messages[-1]

    asyncio.run(scenario())
