from __future__ import annotations

import argparse
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amadeus_bot.character import AutonomyAction, SpontaneityAction
from amadeus_bot.runtime import (
    MemoryReviewLabel,
    RoutingReviewLabel,
    SQLiteAutonomyRuntimeStore,
    SQLiteSpontaneityStore,
    SQLiteTurnTelemetryStore,
    TurnTelemetryReporter,
)

_SINCE_PATTERN = re.compile(r"^(\d+)(h|d|w)$", re.IGNORECASE)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and review Amadeus v2 production observations",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report", help="summarize observed turn behavior")
    report.add_argument("--since", default="7d", help="window such as 24h, 7d, or 4w")
    report.add_argument("--chat-id", type=int, default=None)
    report.add_argument(
        "--routing",
        action="store_true",
        help="show recent question text with fast/llm routing and timings",
    )
    report.add_argument(
        "--limit",
        type=int,
        default=20,
        help="number of recent routing rows when --routing is enabled",
    )
    report.add_argument(
        "--slowest",
        type=int,
        default=0,
        help="also show the N slowest visible replies in the window",
    )
    report.add_argument("--data-dir", type=Path, default=None)

    autonomy = subparsers.add_parser(
        "autonomy",
        help="summarize Phase 5.6 autonomy evaluations and confirmed deliveries",
    )
    autonomy.add_argument("--since", default="7d", help="window such as 24h, 7d, or 4w")
    autonomy.add_argument("--chat-id", type=int, default=None)
    autonomy.add_argument(
        "--recent",
        type=int,
        default=10,
        help="number of recent evaluation metadata rows to show",
    )
    autonomy.add_argument("--data-dir", type=Path, default=None)

    spontaneity = subparsers.add_parser(
        "spontaneity",
        help="summarize Phase 5.6.2 delayed-continuation evaluations and deliveries",
    )
    spontaneity.add_argument("--since", default="7d", help="window such as 24h, 7d, or 4w")
    spontaneity.add_argument("--chat-id", type=int, default=None)
    spontaneity.add_argument(
        "--recent",
        type=int,
        default=10,
        help="number of recent continuation evaluation rows to show",
    )
    spontaneity.add_argument("--data-dir", type=Path, default=None)

    review = subparsers.add_parser("review", help="annotate one observed turn")
    review.add_argument("--turn", required=True, help="full turn id or unique prefix")
    review.add_argument(
        "--routing",
        choices=[label.value for label in RoutingReviewLabel],
        default=None,
        help="routing review label",
    )
    review.add_argument(
        "--memory",
        choices=[label.value for label in MemoryReviewLabel],
        default=None,
        help="memory failure review label",
    )
    review.add_argument("--note", default=None, help="short operator note")
    review.add_argument("--data-dir", type=Path, default=None)
    return parser


def _resolve_v2_data_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    raw = os.environ.get("AMADEUS_DATA_DIR", "./data").strip() or "./data"
    base = Path(raw).expanduser()
    base = (Path.cwd() / base).resolve() if not base.is_absolute() else base.resolve()
    return base if base.name == "v2" else base / "v2"


def _parse_since(raw: str, *, now: datetime | None = None) -> tuple[datetime, str]:
    normalized = raw.strip().lower()
    match = _SINCE_PATTERN.fullmatch(normalized)
    if match is None:
        raise ValueError("--since must look like 24h, 7d, or 4w")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--since amount must be positive")
    unit = match.group(2)
    if unit == "h":
        delta = timedelta(hours=amount)
    elif unit == "d":
        delta = timedelta(days=amount)
    else:
        delta = timedelta(weeks=amount)
    timestamp = now or datetime.now(UTC)
    return timestamp - delta, normalized


def _report(args: argparse.Namespace) -> None:
    data_dir = _resolve_v2_data_dir(args.data_dir)
    telemetry_path = data_dir / "turn-telemetry.sqlite"
    runtime_path = data_dir / "runtime.sqlite"
    if not telemetry_path.exists():
        raise RuntimeError(f"telemetry database does not exist: {telemetry_path}")
    if not runtime_path.exists():
        raise RuntimeError(f"runtime transcript database does not exist: {runtime_path}")
    if args.limit < 1 or args.limit > 200:
        raise ValueError("--limit must be between 1 and 200")
    if args.slowest < 0 or args.slowest > 200:
        raise ValueError("--slowest must be between 0 and 200")

    since, since_label = _parse_since(args.since)
    store = SQLiteTurnTelemetryStore(telemetry_path)
    try:
        reporter = TurnTelemetryReporter(store, runtime_db_path=runtime_path)
        print(
            reporter.render(
                since,
                since_label=since_label,
                chat_id=args.chat_id,
                routing_limit=args.limit if args.routing else 0,
                slowest=args.slowest,
            )
        )
    finally:
        store.close()


def _autonomy_report(args: argparse.Namespace) -> None:
    data_dir = _resolve_v2_data_dir(args.data_dir)
    autonomy_path = data_dir / "autonomy.sqlite"
    if not autonomy_path.exists():
        raise RuntimeError(f"autonomy database does not exist: {autonomy_path}")
    _validate_observation_args(args)

    since, since_label = _parse_since(args.since)
    store = SQLiteAutonomyRuntimeStore(autonomy_path)
    try:
        evaluations = store.list_evaluations_since(since, chat_id=args.chat_id)
        deliveries = store.list_deliveries_since(since, chat_id=args.chat_id)
    finally:
        store.close()

    silent = sum(item.action is AutonomyAction.SILENT for item in evaluations)
    non_silent = len(evaluations) - silent
    silent_pct = (silent / len(evaluations) * 100.0) if evaluations else 0.0
    actions = Counter(item.action.value for item in evaluations)
    reasons = Counter(item.reason_label for item in evaluations)

    lines = [
        "Amadeus Phase 5.6 autonomy observation",
        f"window={since_label}",
        f"evaluations={len(evaluations)}",
        f"silent={silent} ({silent_pct:.1f}%)",
        f"non_silent_candidates={non_silent}",
        f"confirmed_deliveries={len(deliveries)}",
        "actions=" + _render_counter(actions),
        "reasons=" + _render_counter(reasons),
    ]
    if args.recent and evaluations:
        lines.extend(("", f"Recent evaluations (latest {min(args.recent, len(evaluations))})"))
        for item in evaluations[: args.recent]:
            selected = item.selected_signal_id or "-"
            lines.append(
                f"{item.evaluated_at.isoformat()} action={item.action.value} "
                f"reason={item.reason_label} motivation={item.motivation:.2f} "
                f"signal={selected} eval={item.evaluation_id}"
            )
    print("\n".join(lines))


def _spontaneity_report(args: argparse.Namespace) -> None:
    data_dir = _resolve_v2_data_dir(args.data_dir)
    path = data_dir / "spontaneity.sqlite"
    if not path.exists():
        raise RuntimeError(f"spontaneity database does not exist: {path}")
    _validate_observation_args(args)

    since, since_label = _parse_since(args.since)
    store = SQLiteSpontaneityStore(path)
    try:
        evaluations = store.list_evaluations_since(since, chat_id=args.chat_id)
        deliveries = store.list_deliveries_since(since, chat_id=args.chat_id)
    finally:
        store.close()

    silent = sum(item.action is SpontaneityAction.SILENT for item in evaluations)
    continued = len(evaluations) - silent
    silent_pct = (silent / len(evaluations) * 100.0) if evaluations else 0.0
    reasons = Counter(item.reason_label for item in evaluations)
    lines = [
        "Amadeus Phase 5.6.2 spontaneity observation",
        f"window={since_label}",
        f"evaluations={len(evaluations)}",
        f"silent={silent} ({silent_pct:.1f}%)",
        f"continue_candidates={continued}",
        f"confirmed_deliveries={len(deliveries)}",
        "reasons=" + _render_counter(reasons),
    ]
    if args.recent and evaluations:
        lines.extend(("", f"Recent evaluations (latest {min(args.recent, len(evaluations))})"))
        for item in evaluations[: args.recent]:
            lines.append(
                f"{item.evaluated_at.isoformat()} action={item.action.value} "
                f"reason={item.reason_label} motivation={item.motivation:.2f} "
                f"delay={item.delay_seconds:.1f}s source_turn={item.source_turn_id} "
                f"eval={item.evaluation_id}"
            )
    print("\n".join(lines))


def _validate_observation_args(args: argparse.Namespace) -> None:
    if args.chat_id is not None and args.chat_id <= 0:
        raise ValueError("--chat-id must be positive")
    if args.recent < 0 or args.recent > 200:
        raise ValueError("--recent must be between 0 and 200")


def _render_counter(values: Counter[str]) -> str:
    if not values:
        return "none"
    return ",".join(f"{name}:{count}" for name, count in sorted(values.items()))


def _review(args: argparse.Namespace) -> None:
    data_dir = _resolve_v2_data_dir(args.data_dir)
    telemetry_path = data_dir / "turn-telemetry.sqlite"
    if not telemetry_path.exists():
        raise RuntimeError(f"telemetry database does not exist: {telemetry_path}")
    if args.routing is None and args.memory is None and args.note is None:
        raise ValueError("review requires --routing, --memory, or --note")

    store = SQLiteTurnTelemetryStore(telemetry_path)
    try:
        review = store.set_review(
            args.turn,
            routing_label=(RoutingReviewLabel(args.routing) if args.routing is not None else None),
            memory_label=(MemoryReviewLabel(args.memory) if args.memory is not None else None),
            note=args.note,
        )
        print(
            "review_saved "
            f"turn={review.turn_id} "
            f"routing={review.routing_label.value if review.routing_label is not None else '-'} "
            f"memory={review.memory_label.value if review.memory_label is not None else '-'}"
        )
    finally:
        store.close()


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "report":
            _report(args)
        elif args.command == "autonomy":
            _autonomy_report(args)
        elif args.command == "spontaneity":
            _spontaneity_report(args)
        else:
            _review(args)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Amadeus observation failed: {exc}") from None


if __name__ == "__main__":
    main()
