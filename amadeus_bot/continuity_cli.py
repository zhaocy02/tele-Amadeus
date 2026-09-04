from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from amadeus_bot.continuity_audit import audit_telegram_continuity_export
from amadeus_bot.runtime.continuity import import_telegram_continuity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a Telegram Desktop JSON chat export into Amadeus v2 continuity state"
    )
    parser.add_argument("--telegram-export", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument(
        "--since",
        required=True,
        help="timezone-aware ISO-8601 cutoff, e.g. 2026-09-01T22:36:00+08:00",
    )
    parser.add_argument(
        "--confirm-private-export",
        action="store_true",
        help="acknowledge that the input is a private local Telegram export",
    )
    return parser


async def _run(args: argparse.Namespace) -> None:
    if not args.confirm_private_export:
        raise RuntimeError("continuity import requires --confirm-private-export")
    since = datetime.fromisoformat(args.since)
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("--since must include an explicit timezone offset")

    audit = audit_telegram_continuity_export(
        export_path=args.telegram_export,
        since=since,
    )
    audit.ensure_complete()

    report = await import_telegram_continuity(
        export_path=args.telegram_export,
        target_data_dir=args.target_dir,
        chat_id=args.chat_id,
        since=since,
    )
    if audit.text_messages != report.source_messages:
        raise RuntimeError(
            "continuity audit/import selected-message count mismatch: "
            f"audit={audit.text_messages}, import={report.source_messages}"
        )

    report_path = args.target_dir.expanduser().resolve() / "continuity-import-report.json"
    report_path.write_text(report.to_json(), encoding="utf-8")
    report_path.chmod(0o600)
    print(
        "V1 conversation continuity import passed; "
        f"ordinary_messages_after_cutoff={audit.ordinary_messages_after_cutoff}; "
        f"service_events_after_cutoff={audit.service_events_after_cutoff}; "
        f"unsupported_nontext_messages={audit.unsupported_nontext_messages}; "
        f"attachment_messages={audit.attachment_messages}; "
        f"source_messages={report.source_messages}; "
        f"user_messages={report.user_messages}; "
        f"assistant_messages={report.assistant_messages}; "
        f"first_created={report.first_pass.created}; "
        f"second_created={report.second_pass.created}; "
        f"second_unchanged={report.second_pass.unchanged}; "
        f"target_messages={report.target_messages}; "
        f"episode_memories={report.episode_memories}; "
        f"relationship_marker={str(report.relationship_marker_written).lower()}; "
        f"report={report_path}."
    )


def main() -> None:
    args = _parser().parse_args()
    try:
        asyncio.run(_run(args))
    except (OSError, RuntimeError, ValueError, UnicodeError, KeyError) as exc:
        raise SystemExit(f"Amadeus continuity import failed: {exc}") from None


if __name__ == "__main__":
    main()
