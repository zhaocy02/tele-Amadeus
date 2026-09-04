from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from amadeus_bot.memory import MemoryKind, StructuredMemoryRepository
from amadeus_bot.runtime.continuity import import_telegram_continuity
from amadeus_bot.runtime.session_store import ConversationSessionStore


def _write_export(path: Path, messages: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"name": "Amadeus", "type": "personal_chat", "messages": messages}),
        encoding="utf-8",
    )


def _message(
    message_id: int,
    sender_id: int,
    unix_time: int,
    text: object,
) -> dict[str, object]:
    return {
        "id": message_id,
        "type": "message",
        "date_unixtime": str(unix_time),
        "from_id": f"user{sender_id}",
        "text": text,
    }


def test_import_preserves_selected_transcript_and_is_idempotent(tmp_path: Path) -> None:
    export = tmp_path / "result.json"
    target = tmp_path / "v2"
    user_id = 111
    bot_id = 222
    cutoff = datetime.fromisoformat("2026-09-01T22:36:00+08:00")
    before = int(datetime.fromisoformat("2026-09-01T22:35:59+08:00").timestamp())
    first = int(datetime.fromisoformat("2026-09-01T22:36:00+08:00").timestamp())
    second = int(datetime.fromisoformat("2026-09-01T22:37:00+08:00").timestamp())
    third = int(datetime.fromisoformat("2026-09-01T22:38:00+08:00").timestamp())
    _write_export(
        export,
        [
            _message(1, user_id, before, "old"),
            _message(2, user_id, first, "记得这段对话。"),
            _message(3, bot_id, second, [{"type": "plain", "text": "我会记得。"}]),
            {"id": 4, "type": "service", "date_unixtime": str(second), "text": "ignored"},
            _message(5, user_id, third, "之后也要保持连续。"),
        ],
    )

    report = asyncio.run(
        import_telegram_continuity(
            export_path=export,
            target_data_dir=target,
            chat_id=user_id,
            since=cutoff,
        )
    )

    assert report.source_messages == 3
    assert report.user_messages == 2
    assert report.assistant_messages == 1
    assert report.inferred_bot_sender_id == bot_id
    assert report.first_pass.created == 3
    assert report.second_pass.created == 0
    assert report.second_pass.unchanged == 3
    assert report.target_messages == 3
    assert report.episode_memories == 1
    assert report.relationship_marker_written is True

    db = sqlite3.connect(target / "runtime.sqlite")
    try:
        rows = db.execute(
            "SELECT role, content, created_at FROM conversation_messages ORDER BY id"
        ).fetchall()
        provenance = db.execute(
            """
            SELECT telegram_message_id, sender_id
            FROM legacy_telegram_message_provenance ORDER BY telegram_message_id
            """
        ).fetchall()
    finally:
        db.close()
    assert rows == [
        ("user", "记得这段对话。", first),
        ("assistant", "我会记得。", second),
        ("user", "之后也要保持连续。", third),
    ]
    assert provenance == [(2, user_id), (3, bot_id), (5, user_id)]

    memory = StructuredMemoryRepository(target / "structured-memory.sqlite")
    try:
        active = asyncio.run(memory.list_active(limit=20))
    finally:
        memory.close()
    assert any(item.kind is MemoryKind.EPISODE for item in active)
    marker = next(item for item in active if item.kind is MemoryKind.RELATIONSHIP)
    assert "切换到 v2 不代表初次见面" in marker.content

    second_report = asyncio.run(
        import_telegram_continuity(
            export_path=export,
            target_data_dir=target,
            chat_id=user_id,
            since=cutoff,
        )
    )
    assert second_report.first_pass.created == 0
    assert second_report.first_pass.unchanged == 3
    assert second_report.second_pass.created == 0
    assert second_report.relationship_marker_written is False


def test_import_rejects_target_with_unmanaged_transcript(tmp_path: Path) -> None:
    export = tmp_path / "result.json"
    target = tmp_path / "v2"
    cutoff = datetime.fromisoformat("2026-09-01T22:36:00+08:00")
    timestamp = int(cutoff.timestamp())
    _write_export(
        export,
        [
            _message(10, 111, timestamp, "hello"),
            _message(11, 222, timestamp + 1, "hi"),
        ],
    )
    store = ConversationSessionStore(target / "runtime.sqlite")
    try:
        store.append_exchange(111, "existing", "transcript")
    finally:
        store.close()

    with pytest.raises(RuntimeError, match="non-imported conversation transcript"):
        asyncio.run(
            import_telegram_continuity(
                export_path=export,
                target_data_dir=target,
                chat_id=111,
                since=cutoff,
            )
        )


def test_import_rejects_ambiguous_non_user_senders(tmp_path: Path) -> None:
    export = tmp_path / "result.json"
    cutoff = datetime.fromisoformat("2026-09-01T22:36:00+08:00")
    timestamp = int(cutoff.timestamp())
    _write_export(
        export,
        [
            _message(1, 111, timestamp, "hello"),
            _message(2, 222, timestamp + 1, "bot one"),
            _message(3, 333, timestamp + 2, "unexpected sender"),
        ],
    )

    with pytest.raises(RuntimeError, match="exactly one non-user sender"):
        asyncio.run(
            import_telegram_continuity(
                export_path=export,
                target_data_dir=tmp_path / "v2",
                chat_id=111,
                since=cutoff,
            )
        )
