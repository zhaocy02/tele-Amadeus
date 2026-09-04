from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .adapter import IncomingImageAttachment, IncomingMessage
from .updates import parse_authorized_message_update

LOGGER = logging.getLogger(__name__)


class TelegramPollingGateway(Protocol):
    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 25,
    ) -> tuple[Mapping[str, object], ...]: ...


class TelegramMessageHandler(Protocol):
    async def handle(self, message: IncomingMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class InboxMessage:
    update_id: int
    message: IncomingMessage


class TelegramInboxStore:
    """Durable pre-ack inbox for authorized Telegram text/photo messages."""

    def __init__(self, filename: Path) -> None:
        filename.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_inbox (
              update_id INTEGER PRIMARY KEY,
              chat_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              telegram_message_id INTEGER NOT NULL,
              text TEXT NOT NULL,
              attachments_json TEXT NOT NULL DEFAULT '[]',
              received_at INTEGER NOT NULL,
              status TEXT NOT NULL
                CHECK(status IN ('pending', 'processing', 'completed', 'failed')),
              attempts INTEGER NOT NULL DEFAULT 0,
              updated_at INTEGER NOT NULL,
              UNIQUE(chat_id, telegram_message_id)
            )
            """
        )
        columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(telegram_inbox)").fetchall()
        }
        if "attachments_json" not in columns:
            self._db.execute(
                "ALTER TABLE telegram_inbox "
                "ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'"
            )
        self._db.commit()
        self.recover_processing()

    def close(self) -> None:
        self._db.close()

    def recover_processing(self) -> None:
        with self._db:
            self._db.execute(
                "UPDATE telegram_inbox SET status = 'pending' WHERE status = 'processing'"
            )

    def enqueue(self, update_id: int, message: IncomingMessage) -> bool:
        if update_id < 0:
            raise ValueError("Telegram update_id must not be negative")
        timestamp = int(datetime.now(UTC).timestamp())
        try:
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO telegram_inbox(
                      update_id, chat_id, user_id, telegram_message_id, text, attachments_json,
                      received_at, status, attempts, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)
                    """,
                    (
                        update_id,
                        str(message.chat_id),
                        str(message.user_id),
                        message.message_id,
                        message.text,
                        self._attachments_json(message.images),
                        int(message.received_at.timestamp()),
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def pending(self, limit: int = 100) -> tuple[InboxMessage, ...]:
        safe_limit = max(1, min(1000, limit))
        rows = self._db.execute(
            """
            SELECT update_id, chat_id, user_id, telegram_message_id, text,
                   attachments_json, received_at
            FROM telegram_inbox
            WHERE status = 'pending'
            ORDER BY update_id
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return tuple(self._row_to_message(row) for row in rows)

    def mark_processing(self, update_id: int) -> bool:
        with self._db:
            cursor = self._db.execute(
                """
                UPDATE telegram_inbox
                SET status = 'processing', attempts = attempts + 1, updated_at = ?
                WHERE update_id = ? AND status = 'pending'
                """,
                (int(datetime.now(UTC).timestamp()), update_id),
            )
        return bool(cursor.rowcount)

    def mark_pending(self, update_id: int) -> None:
        with self._db:
            self._db.execute(
                """
                UPDATE telegram_inbox SET status = 'pending', updated_at = ?
                WHERE update_id = ? AND status = 'processing'
                """,
                (int(datetime.now(UTC).timestamp()), update_id),
            )

    def mark_completed(self, update_id: int) -> None:
        self._set_terminal_status(update_id, "completed")

    def mark_failed(self, update_id: int) -> None:
        self._set_terminal_status(update_id, "failed")

    def _set_terminal_status(self, update_id: int, status: str) -> None:
        with self._db:
            self._db.execute(
                """
                UPDATE telegram_inbox SET status = ?, updated_at = ?
                WHERE update_id = ? AND status = 'processing'
                """,
                (status, int(datetime.now(UTC).timestamp()), update_id),
            )

    @staticmethod
    def _attachments_json(images: tuple[IncomingImageAttachment, ...]) -> str:
        return json.dumps(
            [
                {
                    "source_id": image.source_id,
                    "width": image.width,
                    "height": image.height,
                    "byte_size_hint": image.byte_size_hint,
                    "media_type": image.media_type,
                }
                for image in images
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _attachments_from_json(raw: str) -> tuple[IncomingImageAttachment, ...]:
        try:
            value: object = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        if not isinstance(value, list):
            return ()
        images: list[IncomingImageAttachment] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            source_id = item.get("source_id")
            width = item.get("width")
            height = item.get("height")
            byte_size_hint = item.get("byte_size_hint")
            media_type = item.get("media_type")
            if not isinstance(source_id, str):
                continue
            if not isinstance(width, int) or not isinstance(height, int):
                continue
            try:
                images.append(
                    IncomingImageAttachment(
                        source_id=source_id,
                        width=width,
                        height=height,
                        byte_size_hint=(
                            byte_size_hint if isinstance(byte_size_hint, int) else None
                        ),
                        media_type=media_type if isinstance(media_type, str) else "image/jpeg",
                    )
                )
            except ValueError:
                continue
        return tuple(images)

    @classmethod
    def _row_to_message(cls, row: sqlite3.Row) -> InboxMessage:
        return InboxMessage(
            update_id=int(row["update_id"]),
            message=IncomingMessage(
                chat_id=int(row["chat_id"]),
                user_id=int(row["user_id"]),
                message_id=int(row["telegram_message_id"]),
                text=str(row["text"]),
                received_at=datetime.fromtimestamp(int(row["received_at"]), tz=UTC),
                images=cls._attachments_from_json(str(row["attachments_json"])),
            ),
        )


class V2TelegramPollingRunner:
    """Long-poll Telegram, durably stage authorized updates, then route them concurrently."""

    def __init__(
        self,
        *,
        gateway: TelegramPollingGateway,
        handler: TelegramMessageHandler,
        inbox: TelegramInboxStore,
        allowed_user_ids: Set[int],
        poll_timeout_seconds: int = 25,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        if not 0 <= poll_timeout_seconds <= 50:
            raise ValueError("poll_timeout_seconds must be between 0 and 50")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must not be negative")
        self._gateway = gateway
        self._handler = handler
        self._inbox = inbox
        self._allowed_user_ids = allowed_user_ids
        self._poll_timeout_seconds = poll_timeout_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._active: dict[int, asyncio.Task[None]] = {}
        self._completed_count = 0

    async def run(
        self,
        *,
        stop_event: asyncio.Event,
        max_completed_messages: int | None = None,
        initial_offset: int | None = None,
    ) -> int:
        if max_completed_messages is not None and max_completed_messages <= 0:
            raise ValueError("max_completed_messages must be positive")
        offset = initial_offset
        await self._dispatch_pending(stop_event, max_completed_messages)

        try:
            while not stop_event.is_set():
                try:
                    updates = await self._gateway.get_updates(
                        offset=offset,
                        timeout_seconds=self._poll_timeout_seconds,
                    )
                except Exception as exc:
                    LOGGER.warning("Telegram getUpdates failed: %s", type(exc).__name__)
                    await self._wait_or_stop(stop_event, self._retry_delay_seconds)
                    continue

                highest_update_id: int | None = None
                for update in updates:
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int) or update_id < 0:
                        LOGGER.warning(
                            "Ignoring malformed Telegram update without integer update_id"
                        )
                        continue
                    highest_update_id = (
                        update_id
                        if highest_update_id is None
                        else max(highest_update_id, update_id)
                    )
                    message = parse_authorized_message_update(update, self._allowed_user_ids)
                    if message is not None:
                        self._inbox.enqueue(update_id, message)

                # The next getUpdates call acknowledges only after every authorized update in this
                # batch has already been committed to the durable local inbox.
                if highest_update_id is not None:
                    offset = highest_update_id + 1

                await self._dispatch_pending(stop_event, max_completed_messages)
        finally:
            await self._drain_active()
        return self._completed_count

    async def _dispatch_pending(
        self,
        stop_event: asyncio.Event,
        max_completed_messages: int | None,
    ) -> None:
        for item in self._inbox.pending():
            if stop_event.is_set():
                break
            if item.update_id in self._active:
                continue
            if not self._inbox.mark_processing(item.update_id):
                continue
            task = asyncio.create_task(
                self._handle_inbox_message(
                    item,
                    stop_event=stop_event,
                    max_completed_messages=max_completed_messages,
                )
            )
            self._active[item.update_id] = task

    async def _handle_inbox_message(
        self,
        item: InboxMessage,
        *,
        stop_event: asyncio.Event,
        max_completed_messages: int | None,
    ) -> None:
        try:
            await self._handler.handle(item.message)
        except asyncio.CancelledError:
            self._inbox.mark_pending(item.update_id)
            raise
        except Exception as exc:
            LOGGER.error(
                "Telegram inbox handler failed update_id=%s type=%s",
                item.update_id,
                type(exc).__name__,
            )
            self._inbox.mark_failed(item.update_id)
        else:
            self._inbox.mark_completed(item.update_id)
            self._completed_count += 1
            if (
                max_completed_messages is not None
                and self._completed_count >= max_completed_messages
            ):
                stop_event.set()
        finally:
            self._active.pop(item.update_id, None)

    async def _drain_active(self) -> None:
        tasks = tuple(self._active.values())
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=20.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)

    @staticmethod
    async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return
