"""Paid hints and the automatic free hint for the opponent.

Rules
-----
* Each side has ``PAID_HINTS_PER_SIDE`` (3) paid hints for the *whole* match.
* Hints within a room are progressive (1 -> 2 -> 3). Progress is tracked per
  side per room in ``hint_progress``, shared by paid and free hints, so a side
  never receives the same hint twice.
* Using a paid hint grants the opposing side one free hint for the room *it*
  is currently in - unless it has already escaped or already has all three
  hints for that room. Free hints never touch the paid allowance.
* If a side already has every hint for its current room, /hint is refused
  without spending a paid hint.
"""

from __future__ import annotations

import sqlite3
import time

import messages as msg
from config import ESCAPED_ROOM, HINTS_PER_ROOM, PAID_HINTS_PER_SIDE, Settings
from database import Database
from game_engine import Outcome
from notifications import enqueue_text
from state import Clock, GameError, PuzzleCache, display_name, get_team, get_user, resolve_side


def hints_revealed(conn: sqlite3.Connection, team_id: int, room: int) -> int:
    row = conn.execute(
        "SELECT hints_revealed FROM hint_progress WHERE team_id = ? AND room = ?", (team_id, room)
    ).fetchone()
    return row["hints_revealed"] if row else 0


def _reveal_next(conn: sqlite3.Connection, team_id: int, room: int) -> int:
    """Advance hint progress for (team, room). Returns the 0-based hint index."""
    index = hints_revealed(conn, team_id, room)
    conn.execute(
        """
        INSERT INTO hint_progress (team_id, room, hints_revealed) VALUES (?, ?, 1)
        ON CONFLICT (team_id, room) DO UPDATE SET hints_revealed = hints_revealed + 1
        """,
        (team_id, room),
    )
    return index


class HintManager:
    def __init__(self, db: Database, settings: Settings, clock: Clock = time.time,
                 puzzles: PuzzleCache | None = None):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.puzzles = puzzles or PuzzleCache()

    def request_hint(self, user_id: int, chat_id: int, chat_type: str) -> Outcome:
        try:
            with self.db.transaction() as conn:
                ctx = resolve_side(conn, user_id, chat_id, chat_type, require_team_chat=True)
                return self._request(conn, ctx)
        except GameError as exc:
            return Outcome(exc.message, exc.buttons)

    def _request(self, conn: sqlite3.Connection, ctx) -> Outcome:
        now = self.clock()
        match, team, opp = ctx.match, ctx.team, ctx.opponent
        match_id = match["match_id"]
        if match["status"] == "lobby":
            raise GameError("⏳ The match hasn't started yet.")
        if match["status"] != "active":
            raise GameError("🏁 This match is over - hints are no longer available.")
        room = team["current_room"]
        if room >= ESCAPED_ROOM:
            raise GameError("🏁 You've already escaped!")
        if team["paid_hints_used"] >= PAID_HINTS_PER_SIDE:
            raise GameError(f"🚫 No paid hints left - you've used all {PAID_HINTS_PER_SIDE}.")
        if hints_revealed(conn, team["team_id"], room) >= HINTS_PER_ROOM:
            raise GameError(
                f"💡 You already have all {HINTS_PER_ROOM} hints for this room - no paid hint was used."
            )

        cur = conn.execute(
            "UPDATE teams SET paid_hints_used = paid_hints_used + 1 "
            "WHERE team_id = ? AND paid_hints_used < ?",
            (team["team_id"], PAID_HINTS_PER_SIDE),
        )
        if cur.rowcount != 1:
            raise GameError(f"🚫 No paid hints left - you've used all {PAID_HINTS_PER_SIDE}.")
        index = _reveal_next(conn, team["team_id"], room)
        event = conn.execute(
            """
            INSERT INTO hint_events (match_id, team_id, room, hint_index, kind, requested_by, created_at)
            VALUES (?, ?, ?, ?, 'paid', ?, ?)
            """,
            (match_id, team["team_id"], room, index, ctx.user_id, now),
        ).lastrowid
        puzzles = self.puzzles.for_match(match)

        # Free hint for the opponent, for the room *they* are in.
        opp_room = opp["current_room"]
        opponent_got_free = False
        if opp_room < ESCAPED_ROOM:
            if hints_revealed(conn, opp["team_id"], opp_room) < HINTS_PER_ROOM:
                opp_index = _reveal_next(conn, opp["team_id"], opp_room)
                conn.execute(
                    "UPDATE teams SET free_hints_received = free_hints_received + 1 WHERE team_id = ?",
                    (opp["team_id"],),
                )
                free_event = conn.execute(
                    """
                    INSERT INTO hint_events
                        (match_id, team_id, room, hint_index, kind, triggered_by_team_id, created_at)
                    VALUES (?, ?, ?, ?, 'free', ?, ?)
                    """,
                    (match_id, opp["team_id"], opp_room, opp_index, team["team_id"], now),
                ).lastrowid
                enqueue_text(
                    conn,
                    chat_id=opp["chat_id"],
                    text=msg.free_hint_message(opp_room, opp_index + 1,
                                               puzzles.get(opp_room).hints[opp_index], match["mode"]),
                    dedupe_key=f"hint:{free_event}",
                    now=now, match_id=match_id, kind="free_hint",
                )
                opponent_got_free = True
            else:
                enqueue_text(
                    conn,
                    chat_id=opp["chat_id"],
                    text=msg.opponent_used_hint_no_free(match["mode"]),
                    dedupe_key=f"hint:{event}:nofree",
                    now=now, match_id=match_id, kind="hint_notice",
                )

        team = get_team(conn, team["team_id"])
        requested_by = display_name(get_user(conn, ctx.user_id)) if match["mode"] == "2v2" else None
        enqueue_text(
            conn,
            chat_id=team["chat_id"],
            text=msg.paid_hint_message(
                room, index + 1, puzzles.get(room).hints[index],
                PAID_HINTS_PER_SIDE - team["paid_hints_used"], requested_by, opponent_got_free,
            ),
            dedupe_key=f"hint:{event}",
            now=now, match_id=match_id, kind="paid_hint",
        )
        return Outcome(None, data={"hint_index": index, "opponent_got_free": opponent_got_free})
