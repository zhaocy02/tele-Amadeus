from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SENDER_ID_RE = re.compile(r"^(?:user)?(\d+)$")
_ATTACHMENT_KEYS = frozenset(
    {
        "photo",
        "file",
        "thumbnail",
        "sticker_emoji",
        "media_type",
        "mime_type",
        "contact_information",
        "location_information",
        "poll",
    }
)


@dataclass(frozen=True, slots=True)
class TelegramContinuityAudit:
    ordinary_messages_after_cutoff: int
    text_messages: int
    service_events_after_cutoff: int
    other_events_after_cutoff: int
    unsupported_nontext_messages: int
    attachment_messages: int
    invalid_message_records: int

    def ensure_complete(self) -> None:
        if self.invalid_message_records:
            raise RuntimeError(
                "Telegram continuity export contains ordinary message records with invalid "
                f"id/sender/date fields after cutoff: {self.invalid_message_records}"
            )
        if self.unsupported_nontext_messages:
            raise RuntimeError(
                "Telegram continuity export contains non-text ordinary messages after cutoff; "
                "continuity import would be incomplete: "
                f"{self.unsupported_nontext_messages}"
            )
        if self.attachment_messages:
            raise RuntimeError(
                "Telegram continuity export contains attachment/media messages after cutoff; "
                "the current transcript importer preserves text but not attachment payloads: "
                f"{self.attachment_messages}"
            )
        if self.text_messages != self.ordinary_messages_after_cutoff:
            raise RuntimeError("Telegram continuity completeness accounting mismatch")


def audit_telegram_continuity_export(
    *,
    export_path: Path,
    since: datetime,
) -> TelegramContinuityAudit:
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("continuity cutoff must be timezone-aware")
    source = export_path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"Telegram export is not a file: {source}")

    document = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
        raise ValueError("Telegram Desktop export must contain a top-level messages array")

    ordinary = 0
    text_messages = 0
    service_events = 0
    other_events = 0
    unsupported_nontext = 0
    attachments = 0
    invalid = 0
    cutoff_utc = since.astimezone(UTC)

    for raw in document["messages"]:
        if not isinstance(raw, dict):
            other_events += 1
            continue
        try:
            created_at = _parse_message_datetime(raw)
        except ValueError:
            if raw.get("type") == "message":
                invalid += 1
            continue
        if created_at < cutoff_utc:
            continue

        event_type = raw.get("type")
        if event_type == "service":
            service_events += 1
            continue
        if event_type != "message":
            other_events += 1
            continue

        ordinary += 1
        message_id = raw.get("id")
        sender_id = _parse_sender_id(raw.get("from_id"))
        if not isinstance(message_id, int) or message_id <= 0 or sender_id is None:
            invalid += 1
            continue

        content = _flatten_text(raw.get("text"))
        if not content.strip():
            unsupported_nontext += 1
            continue
        if any(key in raw and raw.get(key) not in (None, "", [], {}) for key in _ATTACHMENT_KEYS):
            attachments += 1
            continue
        text_messages += 1

    return TelegramContinuityAudit(
        ordinary_messages_after_cutoff=ordinary,
        text_messages=text_messages,
        service_events_after_cutoff=service_events,
        other_events_after_cutoff=other_events,
        unsupported_nontext_messages=unsupported_nontext,
        attachment_messages=attachments,
        invalid_message_records=invalid,
    )


def _parse_sender_id(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = _SENDER_ID_RE.fullmatch(value.strip())
    return int(match.group(1)) if match is not None else None


def _parse_message_datetime(raw: dict[str, object]) -> datetime:
    unix_value = raw.get("date_unixtime")
    if isinstance(unix_value, (str, int)) and str(unix_value).isdigit():
        return datetime.fromtimestamp(int(unix_value), tz=UTC)
    date_value = raw.get("date")
    if not isinstance(date_value, str):
        raise ValueError("Telegram message is missing date/date_unixtime")
    parsed = datetime.fromisoformat(date_value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Telegram export message date must include timezone information")
    return parsed.astimezone(UTC)


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(str(item["text"]))
    return "".join(parts)
