"""Room progression, answers, winner detection, forfeits, status and recovery.

Every public method runs in a single ``BEGIN IMMEDIATE`` transaction and
returns an :class:`Outcome`. Messages to other chats (next room, opponent
alerts, results) are queued in the outbox inside that same transaction;
``Outcome.text`` is the direct reply to the user who triggered the action.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field

import messages as msg
from answer_validator import canonical, is_correct, validate_guess
from config import ESCAPED_ROOM, PAID_HINTS_PER_SIDE, TOTAL_ROOMS, Settings
from database import Database
from notifications import enqueue_photo, enqueue_text
from state import (
    Button,
    Clock,
    GameError,
    PuzzleCache,
    SideContext,
    display_name,
    get_match,
    get_members,
    get_team,
    get_teams,
    get_user,
    migrate_chat_id,
    release_groups,
    resolve_side,
    side_name,
)

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    text: str | None = None
    buttons: list[list[Button]] | None = None
    data: dict = field(default_factory=dict)


def enqueue_room(
    conn: sqlite3.Connection,
    puzzles: PuzzleCache,
    match: sqlite3.Row,
    team: sqlite3.Row,
    room: int,
    now: float,
    header: str | None = None,
) -> None:
    """Queue delivery of ``room`` to ``team`` exactly once (idempotent)."""
    if room > TOTAL_ROOMS:
        return
    puzzle = puzzles.for_match(match).get(room)
    key = f"room:{team['team_id']}:{room}"
    enqueue_text(
        conn,
        chat_id=team["chat_id"],
        text=msg.room_message(puzzle, match["mode"], header),
        dedupe_key=key,
        now=now,
        match_id=match["match_id"],
        kind="room",
    )
    if puzzle.image:
        enqueue_photo(
            conn,
            chat_id=team["chat_id"],
            image=puzzle.image,
            caption=puzzle.image_caption,
            dedupe_key=f"{key}:image",
            now=now,
            match_id=match["match_id"],
            kind="room_image",
        )


def record_result(
    conn: sqlite3.Connection,
    match: sqlite3.Row,
    winner: sqlite3.Row,
    loser: sqlite3.Row,
    result_type: str,
    now: float,
    forfeited_by: int | None = None,
) -> float:
    duration = now - match["started_at"]
    conn.execute(
        """
        INSERT INTO match_results (
            match_id, winner_team_id, loser_team_id, result_type, duration_seconds,
            winner_paid_hints_used, winner_free_hints_received,
            winner_room_reached, loser_room_reached, forfeited_by, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match["match_id"], winner["team_id"], loser["team_id"], result_type, duration,
            winner["paid_hints_used"], winner["free_hints_received"],
            winner["current_room"], loser["current_room"], forfeited_by, now,
        ),
    )
    return duration


class GameEngine:
    def __init__(self, db: Database, settings: Settings, clock: Clock = time.time,
                 puzzles: PuzzleCache | None = None):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.puzzles = puzzles or PuzzleCache()

    # -- answers ------------------------------------------------------------
    def submit_answer(self, user_id: int, chat_id: int, chat_type: str, guess: str | None) -> Outcome:
        ok, error = validate_guess(guess)
        if not ok:
            return Outcome(error)
        try:
            with self.db.transaction() as conn:
                ctx = resolve_side(conn, user_id, chat_id, chat_type, require_team_chat=True)
                return self._submit(conn, ctx, guess)
        except GameError as exc:
            return Outcome(exc.message, exc.buttons)

    def _submit(self, conn: sqlite3.Connection, ctx: SideContext, guess: str) -> Outcome:
        now = self.clock()
        match, team = ctx.match, ctx.team
        if match["status"] == "lobby":
            raise GameError("⏳ The match hasn't started yet - waiting for all players to join.")
        if match["status"] != "active":
            raise GameError("🏁 This match is over - no more answers are accepted.")
        room = team["current_room"]
        if room >= ESCAPED_ROOM:
            raise GameError("🏁 You've already escaped!")

        recent = conn.execute(
            "SELECT COUNT(*) FROM guess_attempts WHERE team_id = ? AND created_at > ?",
            (team["team_id"], now - 60),
        ).fetchone()[0]
        if recent >= self.settings.answer_rate_limit_per_minute:
            raise GameError("🐢 Slow down! Too many guesses in the last minute - think, then try again.")

        puzzle = self.puzzles.for_match(match).get(room)
        correct = is_correct(guess, puzzle.accepted_answers)
        conn.execute(
            """
            INSERT INTO guess_attempts (match_id, team_id, room, user_id, guess_normalized, is_correct, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (match["match_id"], team["team_id"], room, ctx.user_id, canonical(guess)[:200], int(correct), now),
        )
        if not correct:
            return Outcome(msg.wrong_answer())

        # Compare-and-set: only one advancement per room, ever.
        cur = conn.execute(
            "UPDATE teams SET current_room = current_room + 1 WHERE team_id = ? AND current_room = ?",
            (team["team_id"], room),
        )
        if cur.rowcount != 1:
            raise GameError("✅ That room has already been solved.")
        conn.execute(
            """
            INSERT INTO room_completions (match_id, team_id, room, solved_by, completed_at, elapsed_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (match["match_id"], team["team_id"], room, ctx.user_id, now, now - match["started_at"]),
        )

        if room == TOTAL_ROOMS:
            return self._declare_winner(conn, ctx, now)

        team = get_team(conn, team["team_id"])
        solver = None
        if ctx.mode == "2v2":
            solver = display_name(get_user(conn, ctx.user_id))
        enqueue_room(conn, self.puzzles, match, team, room + 1, now,
                     header=msg.unlock_header(room, solver))
        enqueue_text(
            conn,
            chat_id=ctx.opponent["chat_id"],
            text=msg.opponent_progress(room + 1, ctx.mode),
            dedupe_key=f"progress:{team['team_id']}:{room + 1}:to:{ctx.opponent['team_id']}",
            now=now,
            match_id=match["match_id"],
            kind="progress",
        )
        return Outcome(None, data={"advanced_to": room + 1})

    def _declare_winner(self, conn: sqlite3.Connection, ctx: SideContext, now: float) -> Outcome:
        match_id = ctx.match["match_id"]
        # Atomic winner determination: only the first side to flip the match
        # from 'active' to 'completed' wins. A second side arriving later sees
        # rowcount 0 and its whole transaction is rolled back.
        cur = conn.execute(
            """
            UPDATE matches SET status = 'completed', ended_at = ?, winner_team_id = ?, end_reason = 'escaped'
            WHERE match_id = ? AND status = 'active'
            """,
            (now, ctx.team["team_id"], match_id),
        )
        if cur.rowcount != 1:
            raise GameError("🏁 Too late - this match has already ended.")
        conn.execute("UPDATE teams SET escaped_at = ? WHERE team_id = ?", (now, ctx.team["team_id"]))

        match = get_match(conn, match_id)
        winner = get_team(conn, ctx.team["team_id"])
        loser = get_team(conn, ctx.opponent["team_id"])
        duration = record_result(conn, match, winner, loser, "escaped", now)
        release_groups(conn, match_id, now)

        winner_name = side_name(conn, winner, match["mode"])
        enqueue_text(
            conn,
            chat_id=winner["chat_id"],
            text=msg.victory_message(winner_name, duration, winner["paid_hints_used"],
                                     winner["free_hints_received"]),
            dedupe_key=f"result:{match_id}:{winner['team_id']}",
            now=now, match_id=match_id, kind="result",
        )
        enqueue_text(
            conn,
            chat_id=loser["chat_id"],
            text=msg.defeat_message(winner_name, duration, loser["current_room"]),
            dedupe_key=f"result:{match_id}:{loser['team_id']}",
            now=now, match_id=match_id, kind="result",
        )
        return Outcome(None, data={"winner_team_id": winner["team_id"]})

    def handle_private_text(self, user_id: int, chat_id: int, text: str) -> Outcome:
        """Plain text in a private chat: an answer in 1v1, guidance otherwise."""
        part = self.db.query_one(
            """
            SELECT m.mode, m.status FROM team_members tm JOIN matches m ON m.match_id = tm.match_id
            WHERE tm.user_id = ? AND m.status IN ('lobby', 'active')
            """,
            (user_id,),
        )
        if part is None:
            return Outcome("👋 You're not in a match. Tap below or use /help.",
                           [[Button("🏠 Main menu", callback="menu:home")]])
        if part["status"] == "lobby":
            return Outcome("⏳ Waiting for everyone to join. Use /status to see who's missing.")
        if part["mode"] == "2v2":
            return Outcome("👥 In 2v2, answers go in your team group: /answer &lt;guess&gt;")
        return self.submit_answer(user_id, chat_id, "private", text)

    # -- status ---------------------------------------------------------------
    def status(self, user_id: int, chat_id: int, chat_type: str) -> Outcome:
        try:
            with self.db.transaction() as conn:
                ctx = resolve_side(conn, user_id, chat_id, chat_type, require_team_chat=False)
                return Outcome(self._status_text(conn, ctx))
        except GameError as exc:
            return Outcome(exc.message, exc.buttons)

    def _status_text(self, conn: sqlite3.Connection, ctx: SideContext) -> str:
        match, team, opp = ctx.match, ctx.team, ctx.opponent
        mode = match["mode"]
        mode_label = "1v1 Individual" if mode == "1v1" else "2v2 Team"
        lines = [
            "📊 <b>MATCH STATUS</b>",
            f"Match ID: <code>{match['match_id']}</code>",
            f"Mode: {mode_label}",
        ]
        if match["status"] == "lobby":
            lines.append("Status: ⏳ Waiting for players")
            for t in get_teams(conn, match["match_id"]):
                members = [display_name(u) for u in get_members(conn, t["team_id"])]
                need = t["capacity"] - len(members)
                if mode == "1v1":
                    label = "Player 1" if t["label"] == "A" else "Player 2"
                else:
                    label = f"Team {t['label']}"
                parts = ", ".join(members) if members else "-"
                missing = f" (waiting for {need} more)" if need > 0 else ""
                group = ""
                if mode == "2v2":
                    group = " · group linked ✅" if t["chat_id"] else " · group not linked ❌"
                lines.append(f"• {label}: {parts}{missing}{group}")
            return "\n".join(lines)

        status_label = {"active": "🟢 Active", "completed": "🏁 Finished"}.get(match["status"], match["status"])
        end = match["ended_at"] or self.clock()
        you = "Your team" if mode == "2v2" else "You"
        lines += [
            f"Status: {status_label}",
            f"{you}: {side_name(conn, team, mode)}",
            f"{'Your team' if mode == '2v2' else 'Your'} room: <b>{msg.room_label(team['current_room'])}</b>",
            f"Opponent room: <b>{msg.room_label(opp['current_room'])}</b>",
            f"Paid hints left: <b>{PAID_HINTS_PER_SIDE - team['paid_hints_used']} / {PAID_HINTS_PER_SIDE}</b>",
            f"Free hints received: {team['free_hints_received']}",
            f"Elapsed time: {msg.format_duration(end - match['started_at'])}",
        ]
        return "\n".join(lines)

    # -- quitting ------------------------------------------------------------
    def request_quit(self, user_id: int, chat_id: int, chat_type: str) -> Outcome:
        try:
            with self.db.transaction() as conn:
                ctx = resolve_side(conn, user_id, chat_id, chat_type, require_team_chat=False)
        except GameError as exc:
            return Outcome(exc.message, exc.buttons)
        match = ctx.match
        if match["status"] not in ("lobby", "active"):
            return Outcome("🏁 This match is already over.")
        yes = f"quit:yes:{match['match_id']}:{user_id}"
        no = f"quit:no:{user_id}"
        if match["status"] == "lobby":
            if match["creator_id"] == user_id:
                text = "❓ Cancel this match? Nobody has started playing yet."
            else:
                text = "❓ Leave this lobby? You can rejoin with the code while it's open."
            return Outcome(text, [[Button("Yes, leave", callback=yes), Button("No", callback=no)]])
        rule = ""
        if match["mode"] == "2v2":
            rule = "\nTeam rule: if either teammate quits, the <b>whole team forfeits</b>."
        text = f"⚠️ <b>Forfeit the match?</b> Your opponent will be awarded the win.{rule}"
        return Outcome(text, [[Button("🏳️ Yes, forfeit", callback=yes), Button("Keep playing", callback=no)]])

    def confirm_quit(self, user_id: int, match_id: str) -> Outcome:
        try:
            with self.db.transaction() as conn:
                return self._confirm_quit(conn, user_id, match_id)
        except GameError as exc:
            return Outcome(exc.message, exc.buttons)

    def _confirm_quit(self, conn: sqlite3.Connection, user_id: int, match_id: str) -> Outcome:
        now = self.clock()
        match = get_match(conn, match_id)
        member = conn.execute(
            "SELECT team_id FROM team_members WHERE match_id = ? AND user_id = ?", (match_id, user_id)
        ).fetchone()
        if match is None or member is None:
            raise GameError("You're not part of that match.")
        if match["status"] not in ("lobby", "active"):
            raise GameError("🏁 This match is already over.")
        quitter = get_user(conn, user_id)
        quitter_name = display_name(quitter)

        if match["status"] == "lobby":
            if match["creator_id"] == user_id:
                conn.execute(
                    "UPDATE matches SET status = 'cancelled', end_reason = 'cancelled', ended_at = ? "
                    "WHERE match_id = ? AND status = 'lobby'",
                    (now, match_id),
                )
                release_groups(conn, match_id, now)
                for t in get_teams(conn, match_id):
                    for u in get_members(conn, t["team_id"]):
                        if u["user_id"] != user_id and u["private_chat_id"]:
                            enqueue_text(
                                conn, chat_id=u["private_chat_id"],
                                text=f"❌ Match <code>{match_id}</code> was cancelled by its creator.",
                                dedupe_key=f"cancel:{match_id}:{u['user_id']}", now=now, match_id=match_id,
                            )
                return Outcome("❌ Match cancelled.")
            conn.execute("DELETE FROM team_members WHERE match_id = ? AND user_id = ?", (match_id, user_id))
            remaining = conn.execute(
                "SELECT COUNT(*) FROM team_members WHERE team_id = ?", (member["team_id"],)
            ).fetchone()[0]
            if remaining == 0:
                conn.execute(
                    "UPDATE linked_groups SET released_at = ? WHERE team_id = ? AND released_at IS NULL",
                    (now, member["team_id"]),
                )
                conn.execute("UPDATE teams SET chat_id = NULL WHERE team_id = ? AND capacity = 2",
                             (member["team_id"],))
            return Outcome("👋 You left the lobby.")

        # Active match: forfeit. In 2v2 one teammate quitting forfeits the
        # whole team - we never continue with an incomplete team.
        cur = conn.execute(
            """
            UPDATE matches SET status = 'completed', ended_at = ?, end_reason = 'forfeit',
                winner_team_id = (SELECT team_id FROM teams WHERE match_id = ? AND team_id != ?)
            WHERE match_id = ? AND status = 'active'
            """,
            (now, match_id, member["team_id"], match_id),
        )
        if cur.rowcount != 1:
            raise GameError("🏁 This match is already over.")
        match = get_match(conn, match_id)
        loser = get_team(conn, member["team_id"])
        winner = get_team(conn, match["winner_team_id"])
        duration = record_result(conn, match, winner, loser, "forfeit", now, forfeited_by=user_id)
        release_groups(conn, match_id, now)
        enqueue_text(
            conn, chat_id=winner["chat_id"],
            text=msg.forfeit_winner_message(quitter_name, duration),
            dedupe_key=f"result:{match_id}:{winner['team_id']}", now=now, match_id=match_id, kind="result",
        )
        enqueue_text(
            conn, chat_id=loser["chat_id"],
            text=msg.forfeit_loser_message(quitter_name, match["mode"] == "2v2", duration),
            dedupe_key=f"result:{match_id}:{loser['team_id']}", now=now, match_id=match_id, kind="result",
        )
        return Outcome("🏳️ You forfeited the match.", data={"winner_team_id": winner["team_id"]})

    # -- maintenance ----------------------------------------------------------
    def migrate_chat(self, old_chat_id: int, new_chat_id: int) -> None:
        with self.db.transaction() as conn:
            migrate_chat_id(conn, old_chat_id, new_chat_id)

    def recover(self) -> dict:
        """Called on startup: make sure every active side has its room queued.

        Thanks to dedupe keys this only queues rooms that were never queued
        before; already-delivered puzzles are not re-sent.
        """
        now = self.clock()
        with self.db.transaction() as conn:
            active = conn.execute("SELECT * FROM matches WHERE status = 'active'").fetchall()
            for match in active:
                for team in get_teams(conn, match["match_id"]):
                    enqueue_room(conn, self.puzzles, match, team, team["current_room"], now)
            pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
            lobbies = conn.execute("SELECT COUNT(*) FROM matches WHERE status = 'lobby'").fetchone()[0]
        summary = {"active_matches": len(active), "open_lobbies": lobbies, "pending_messages": pending}
        log.info("Recovered state: %s", summary)
        return summary
