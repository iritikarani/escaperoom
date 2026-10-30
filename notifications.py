"""Reliable, idempotent message delivery (transactional outbox).

Game logic never calls Telegram directly. Instead it *enqueues* messages in
the ``outbox`` table inside the same transaction as the state change that
caused them. :class:`Notifier` then delivers pending rows.

Why:

* A state change and its notifications commit or roll back together, so a
  player can never advance without being sent the next room (or vice versa).
* Each logical message has a ``dedupe_key`` (``room:<team>:<room>``, ...).
  Enqueuing the same key twice is a no-op, so restarts, retries and recovery
  never produce duplicate puzzles.
* Delivery failures are retried with backoff and survive restarts.

Delivery is *at-least-once*: if the process dies after Telegram accepted a
message but before it was marked as sent, that one message is re-sent after
restart. Telegram offers no idempotency key for sendMessage, so this is the
safest available trade-off (a duplicate is better than a missing puzzle).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from telegram import LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError

from database import Database
from state import migrate_chat_id

log = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")


def enqueue_text(
    conn: sqlite3.Connection,
    *,
    chat_id: int | None,
    text: str,
    dedupe_key: str,
    now: float,
    match_id: str | None = None,
    kind: str = "text",
) -> bool:
    """Queue an HTML text message. Returns False if the key already existed."""
    return _enqueue(conn, chat_id, match_id, kind, {"type": "text", "text": text}, dedupe_key, now)


def enqueue_photo(
    conn: sqlite3.Connection,
    *,
    chat_id: int | None,
    image: str,
    caption: str | None,
    dedupe_key: str,
    now: float,
    match_id: str | None = None,
    kind: str = "photo",
) -> bool:
    payload = {"type": "photo", "image": image, "caption": caption}
    return _enqueue(conn, chat_id, match_id, kind, payload, dedupe_key, now)


def _enqueue(conn, chat_id, match_id, kind, payload, dedupe_key, now) -> bool:
    if chat_id is None:
        log.warning("Not queueing %s: no chat id", dedupe_key)
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO outbox
            (match_id, chat_id, kind, payload, dedupe_key, created_at, next_attempt_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (match_id, chat_id, kind, json.dumps(payload, ensure_ascii=False), dedupe_key, now, now),
    )
    return cur.rowcount == 1


def _seconds(value: Any) -> float:
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


class Notifier:
    """Delivers pending outbox rows through a ``telegram.Bot``-like object."""

    def __init__(
        self,
        db: Database,
        bot: Any,
        assets_dir: Path,
        *,
        max_attempts: int = 8,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.db = db
        self.bot = bot
        self.assets_dir = Path(assets_dir)
        self.max_attempts = max_attempts
        self.clock = clock
        self.sleep = sleep
        self._lock = asyncio.Lock()
        self._migrated: dict[int, int] = {}

    async def flush(self) -> int:
        """Send everything that is due. Returns the number of messages sent."""
        async with self._lock:
            rows = self.db.query_all(
                """
                SELECT * FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY message_id
                """,
                (self.clock(),),
            )
            if not rows:
                return 0
            by_chat: OrderedDict[int, list[sqlite3.Row]] = OrderedDict()
            for row in rows:
                by_chat.setdefault(row["chat_id"], []).append(row)
            # Different chats are sent concurrently (so both sides receive
            # Room 1 at the same moment); one chat's messages stay in order.
            results = await asyncio.gather(
                *(self._send_chat(chat_rows) for chat_rows in by_chat.values())
            )
            return sum(results)

    async def _send_chat(self, rows: list[sqlite3.Row]) -> int:
        sent = 0
        for row in rows:
            outcome = await self._send_row(row)
            if outcome == "sent":
                sent += 1
            elif outcome == "retry":
                break  # keep later messages for this chat in order
        return sent

    async def _send_row(self, row: sqlite3.Row) -> str:
        payload = json.loads(row["payload"])
        chat_id = self._migrated.get(row["chat_id"], row["chat_id"])
        for _ in range(3):
            try:
                await self._deliver(chat_id, payload)
            except RetryAfter as exc:
                await self.sleep(min(_seconds(exc.retry_after), 30.0))
                continue
            except ChatMigrated as exc:
                new_id = exc.new_chat_id
                log.info("Chat %s migrated to %s", chat_id, new_id)
                with self.db.transaction() as conn:
                    migrate_chat_id(conn, chat_id, new_id)
                self._migrated[row["chat_id"]] = new_id
                chat_id = new_id
                continue
            except Forbidden as exc:
                # The user blocked the bot or the bot was removed from the group.
                return self._fail(row, f"forbidden: {exc}")
            except BadRequest as exc:
                if payload.get("type") == "text" and "parse" in str(exc).lower():
                    # Malformed HTML in puzzle content: fall back to plain text
                    # rather than withholding the puzzle.
                    payload = {"type": "text", "text": _TAG.sub("", payload["text"]), "plain": True}
                    continue
                return self._fail(row, f"bad request: {exc}")
            except FileNotFoundError as exc:
                return self._fail(row, f"missing image: {exc}")
            except TelegramError as exc:  # NetworkError, TimedOut, ...
                return self._retry_later(row, str(exc))
            else:
                with self.db.transaction() as conn:
                    conn.execute(
                        "UPDATE outbox SET status = 'sent', sent_at = ?, attempts = attempts + 1, "
                        "chat_id = ? WHERE message_id = ?",
                        (self.clock(), chat_id, row["message_id"]),
                    )
                return "sent"
        return self._retry_later(row, "rate limited / repeated retry")

    async def _deliver(self, chat_id: int, payload: dict) -> None:
        if payload["type"] == "photo":
            path = (self.assets_dir / payload["image"]).resolve()
            if self.assets_dir.resolve() not in path.parents:
                raise FileNotFoundError(payload["image"])
            with path.open("rb") as fh:
                await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=fh,
                    caption=payload.get("caption"),
                    parse_mode=ParseMode.HTML,
                )
        else:
            await self.bot.send_message(
                chat_id=chat_id,
                text=payload["text"],
                parse_mode=None if payload.get("plain") else ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    def _fail(self, row: sqlite3.Row, error: str) -> str:
        log.warning("Giving up on outbox message %s (%s): %s", row["message_id"], row["kind"], error)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE outbox SET status = 'failed', attempts = attempts + 1, last_error = ? "
                "WHERE message_id = ?",
                (error[:500], row["message_id"]),
            )
        return "failed"

    def _retry_later(self, row: sqlite3.Row, error: str) -> str:
        attempts = row["attempts"] + 1
        if attempts >= self.max_attempts:
            return self._fail(row, f"too many attempts: {error}")
        delay = min(2 ** attempts, 300)
        log.info("Outbox message %s will be retried in %ss: %s", row["message_id"], delay, error)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE outbox SET attempts = ?, last_error = ?, next_attempt_at = ? "
                "WHERE message_id = ?",
                (attempts, error[:500], self.clock() + delay, row["message_id"]),
            )
        return "retry"
