from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from amadeus_bot.continuity_audit import audit_telegram_continuity_export


def _write(path: Path, messages: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"messages": messages}), encoding="utf-8")


def _message(message_id: int, unix_time: int, text: object, **extra: object) -> dict[str, object]:
    return {
        "id": message_id,
        "type": "message",
        "date_unixtime": str(unix_time),
        "from_id": "user111",
        "text": text,
        **extra,
    }


def test_audit_accepts_complete_text_only_export(tmp_path: Path) -> None:
    export = tmp_path / "result.json"
    cutoff = datetime.fromisoformat("2026-09-01T22:36:00+08:00")
    timestamp = int(cutoff.timestamp())
    _write(
        export,
        [
            _message(1, timestamp - 1, "before"),
            _message(2, timestamp, "hello"),
            {
                "id": 3,
                "type": "service",
                "date_unixtime": str(timestamp + 1),
                "text": "service",
            },
            _message(4, timestamp + 2, [{"type": "plain", "text": "world"}]),
        ],
    )

    audit = audit_telegram_continuity_export(export_path=export, since=cutoff)
    audit.ensure_complete()

    assert audit.ordinary_messages_after_cutoff == 2
    assert audit.text_messages == 2
    assert audit.service_events_after_cutoff == 1
    assert audit.unsupported_nontext_messages == 0
    assert audit.attachment_messages == 0
    assert audit.invalid_message_records == 0


def test_audit_rejects_nontext_message(tmp_path: Path) -> None:
    export = tmp_path / "result.json"
    cutoff = datetime.fromisoformat("2026-09-01T22:36:00+08:00")
    timestamp = int(cutoff.timestamp())
    _write(export, [_message(1, timestamp, "", sticker_emoji="🙂")])

    audit = audit_telegram_continuity_export(export_path=export, since=cutoff)

    with pytest.raises(RuntimeError, match="non-text ordinary messages"):
        audit.ensure_complete()


def test_audit_rejects_attachment_even_with_caption(tmp_path: Path) -> None:
    export = tmp_path / "result.json"
    cutoff = datetime.fromisoformat("2026-09-01T22:36:00+08:00")
    timestamp = int(cutoff.timestamp())
    _write(export, [_message(1, timestamp, "caption", photo="photos/photo_1.jpg")])

    audit = audit_telegram_continuity_export(export_path=export, since=cutoff)

    assert audit.attachment_messages == 1
    with pytest.raises(RuntimeError, match="attachment/media messages"):
        audit.ensure_complete()
