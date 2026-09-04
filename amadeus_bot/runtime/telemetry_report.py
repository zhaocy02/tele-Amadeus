from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .telemetry import SQLiteTurnTelemetryStore, TurnObservation, TurnTelemetryRecord


@dataclass(frozen=True, slots=True)
class LatencyDistribution:
    count: int
    p50: int
    p95: int
    maximum: int


@dataclass(frozen=True, slots=True)
class TurnTelemetrySummary:
    total_turns: int
    fast_turns: int
    llm_turns: int
    turns_with_retrieved_memory: int
    retrospective_turns: int
    warning_turns: int
    fast_response_wait: LatencyDistribution | None
    llm_response_wait: LatencyDistribution | None
    fast_generation: LatencyDistribution | None
    llm_generation: LatencyDistribution | None
    llm_policy: LatencyDistribution | None
    queue_wait: LatencyDistribution | None


class TurnTelemetryReporter:
    """Render local operator reports without duplicating transcript prose into telemetry."""

    def __init__(
        self,
        store: SQLiteTurnTelemetryStore,
        *,
        runtime_db_path: Path,
    ) -> None:
        self._store = store
        self._runtime_db_path = runtime_db_path

    def summarize(
        self,
        since: datetime,
        *,
        chat_id: int | None = None,
    ) -> TurnTelemetrySummary:
        records = self._store.list_since(since, chat_id=chat_id)
        fast = tuple(record for record in records if record.policy_mode == "fast")
        llm = tuple(record for record in records if record.policy_mode == "llm")
        return TurnTelemetrySummary(
            total_turns=len(records),
            fast_turns=len(fast),
            llm_turns=len(llm),
            turns_with_retrieved_memory=sum(
                bool(record.retrieved_memory_ids) for record in records
            ),
            retrospective_turns=sum(record.retrospective_triggered for record in records),
            warning_turns=sum(bool(record.warnings) for record in records),
            fast_response_wait=self._distribution(fast, lambda item: item.response_wait_ms),
            llm_response_wait=self._distribution(llm, lambda item: item.response_wait_ms),
            fast_generation=self._distribution(fast, lambda item: item.generation_ms),
            llm_generation=self._distribution(llm, lambda item: item.generation_ms),
            llm_policy=self._distribution(llm, lambda item: item.policy_ms),
            queue_wait=self._distribution(records, lambda item: item.queue_wait_ms),
        )

    def render(
        self,
        since: datetime,
        *,
        since_label: str,
        chat_id: int | None = None,
        routing_limit: int = 0,
        slowest: int = 0,
    ) -> str:
        records = self._store.list_since(since, chat_id=chat_id)
        summary = self.summarize(since, chat_id=chat_id)
        lines = [
            "Amadeus Phase 5.4 production observation",
            f"window={since_label}",
            f"turns={summary.total_turns}",
            (
                f"fast={summary.fast_turns} "
                f"({self._percentage(summary.fast_turns, summary.total_turns)})"
            ),
            f"llm={summary.llm_turns} ({self._percentage(summary.llm_turns, summary.total_turns)})",
            f"turns_with_retrieved_memory={summary.turns_with_retrieved_memory}",
            f"retrospective_turns={summary.retrospective_turns}",
            f"warning_turns={summary.warning_turns}",
        ]
        self._append_distribution(lines, "fast_response_wait_ms", summary.fast_response_wait)
        self._append_distribution(lines, "llm_response_wait_ms", summary.llm_response_wait)
        self._append_distribution(lines, "fast_generation_ms", summary.fast_generation)
        self._append_distribution(lines, "llm_generation_ms", summary.llm_generation)
        self._append_distribution(lines, "llm_policy_ms", summary.llm_policy)
        self._append_distribution(lines, "queue_wait_ms", summary.queue_wait)

        routing_counts: Counter[str] = Counter()
        memory_counts: Counter[str] = Counter()
        for record in records:
            review = self._store.get_review(record.turn_id)
            if review is None:
                continue
            if review.routing_label is not None:
                routing_counts[review.routing_label.value] += 1
            if review.memory_label is not None:
                memory_counts[review.memory_label.value] += 1
        if routing_counts:
            lines.append(
                "routing_reviews="
                + ",".join(f"{label}:{count}" for label, count in sorted(routing_counts.items()))
            )
        if memory_counts:
            lines.append(
                "memory_reviews="
                + ",".join(f"{label}:{count}" for label, count in sorted(memory_counts.items()))
            )

        if routing_limit > 0:
            lines.extend(("", f"Routing audit (latest {routing_limit})"))
            routing_selected = records[-routing_limit:]
            for record in reversed(routing_selected):
                lines.extend(self._render_observation(self._observation(record)))

        if slowest > 0:
            lines.extend(("", f"Slowest visible replies (top {slowest})"))
            slowest_selected = sorted(
                records,
                key=lambda item: item.response_wait_ms,
                reverse=True,
            )[:slowest]
            for record in slowest_selected:
                lines.extend(self._render_observation(self._observation(record)))

        return "\n".join(lines)

    def _observation(self, record: TurnTelemetryRecord) -> TurnObservation:
        return self._store.observation(record, runtime_db_path=self._runtime_db_path)

    @staticmethod
    def _render_observation(observation: TurnObservation) -> tuple[str, ...]:
        record = observation.telemetry
        review = observation.review
        routing_review = (
            review.routing_label.value
            if review is not None and review.routing_label is not None
            else "-"
        )
        memory_review = (
            review.memory_label.value
            if review is not None and review.memory_label is not None
            else "-"
        )
        question = TurnTelemetryReporter._preview(observation.question_text)
        memory_ids = len(record.retrieved_memory_ids)
        return (
            (
                f"{record.observed_at.isoformat()} "
                f"[{record.policy_mode}/{record.policy_reason_label or record.policy_act}] "
                f"wait={record.response_wait_ms}ms generation={record.generation_ms}ms "
                f"policy={record.policy_ms}ms queue={record.queue_wait_ms}ms "
                f"memories={memory_ids} turn={record.turn_id[:13]}"
            ),
            f"review=routing:{routing_review} memory:{memory_review}",
            f"Q: {question}",
        )

    @staticmethod
    def _preview(text: str | None, limit: int = 240) -> str:
        if text is None:
            return "<transcript unavailable>"
        normalized = " ".join(text.split())
        if len(normalized) <= limit:
            return normalized
        return normalized[: limit - 1].rstrip() + "…"

    @staticmethod
    def _distribution(
        records: tuple[TurnTelemetryRecord, ...],
        getter: Callable[[TurnTelemetryRecord], int],
    ) -> LatencyDistribution | None:
        if not records:
            return None
        values = sorted(getter(record) for record in records)
        return LatencyDistribution(
            count=len(values),
            p50=TurnTelemetryReporter._percentile(values, 0.50),
            p95=TurnTelemetryReporter._percentile(values, 0.95),
            maximum=values[-1],
        )

    @staticmethod
    def _percentile(values: list[int], fraction: float) -> int:
        if not values:
            raise ValueError("percentile requires at least one value")
        if not 0.0 < fraction <= 1.0:
            raise ValueError("percentile fraction must be in (0, 1]")
        rank = max(1, math.ceil(len(values) * fraction))
        return values[rank - 1]

    @staticmethod
    def _append_distribution(
        lines: list[str],
        label: str,
        distribution: LatencyDistribution | None,
    ) -> None:
        if distribution is None:
            lines.append(f"{label}=n/a")
            return
        lines.append(
            f"{label}=p50:{distribution.p50} p95:{distribution.p95} "
            f"max:{distribution.maximum} n:{distribution.count}"
        )

    @staticmethod
    def _percentage(value: int, total: int) -> str:
        if total <= 0:
            return "0.0%"
        return f"{100.0 * value / total:.1f}%"
