"""Users, match creation, joining, 2v2 group linking and match start.

Lobby rules
-----------
* A user can be in at most one open (lobby/active) match at a time.
* The creator is always on side A.
* 1v1: one join code (for side B), used in a private chat with the bot.
* 2v2: one join code per team. Each teammate sends ``/join CODE`` *inside
  their team's group*. The first use links the group to the team; the group
  can then only serve that team, and the team only that group.
* Every 2v2 player must have pressed Start in a private chat with the bot
  first (so the bot knows them and can message them privately).
* The match starts automatically, at one authoritative timestamp, as soon as
  every slot is filled and (2v2) both groups are linked.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from html import escape
from typing import Callable

import messages as msg
from config import TEAM_SIZE, Settings
from database import Database
from game_engine import Outcome, enqueue_room
from notifications import enqueue_text
from puzzle_manager import PuzzleSet
from state import (
    GROUP_CHAT_TYPES,
    Button,
    Clock,
    GameError,
    PuzzleCache,
    display_name,
    find_linked_team,
    find_open_participation,
    get_match,
    get_teams,
    get_user,
    release_groups,
    side_name,
)

log = logging.getLogger(__name__)

# No 0/O, 1/I/L: codes are easy to read aloud and type on a phone.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_PATTERN = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")
MATCH_ID_LENGTH = 6
JOIN_FAILURE_WINDOW = 600


def generate_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def normalize_code(raw: str | None) -> str | None:
    if not raw:
        return None
    code = re.sub(r"[\s-]", "", raw).upper()
    return code if CODE_PATTERN.match(code) else None


@dataclass
class CreatedMatch:
    match_id: str
    mode: str
    code_a: str | None
    code_b: str


class Matchmaking:
    def __init__(self, db: Database, puzzle_source: Callable[[], PuzzleSet], settings: Settings,
                 clock: Clock = time.time, puzzles: PuzzleCache | None = None):
        self.db = db
        self.puzzle_source = puzzle_source
        self.settings = settings
        self.clock = clock
        self.puzzles = puzzles or PuzzleCache()

    # -- users ------------------------------------------------------------
    def register_user(self, user_id: int, first_name: str | None, last_name: str | None,
                      username: str | None, chat_id: int | None, chat_type: str | None) -> None:
        now = self.clock()
        private_chat = chat_id if chat_type == "private" else None
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, private_chat_id,
                                   started_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    private_chat_id = COALESCE(excluded.private_chat_id, users.private_chat_id),
                    started_at = COALESCE(users.started_at, excluded.started_at),
                    updated_at = excluded.updated_at
                """,
                (user_id, username, (first_name or "")[:64], (last_name or "")[:64] or None,
                 private_chat, now if private_chat else None, now, now),
            )

    # -- create -------------------------------------------------------------
    def create_match(self, user_id: int, mode: str) -> CreatedMatch:
        if mode not in TEAM_SIZE:
            raise GameError("Unknown game mode.")
        now = self.clock()
        puzzles = self.puzzle_source()
        with self.db.transaction() as conn:
            user = get_user(conn, user_id)
            if user is None or user["private_chat_id"] is None:
                raise GameError("Please create matches in a private chat with me.")
            existing = find_open_participation(conn, user_id)
            if existing:
                raise GameError(
                    f"🎮 You're already in match <code>{existing['match_id']}</code>. "
                    "Finish it or use /quit before creating a new one."
                )
            match_id = self._unique(conn, "SELECT 1 FROM matches WHERE match_id = ?", MATCH_ID_LENGTH)
            conn.execute(
                """
                INSERT INTO matches (match_id, mode, status, creator_id, puzzle_snapshot, created_at, expires_at)
                VALUES (?, ?, 'lobby', ?, ?, ?, ?)
                """,
                (match_id, mode, user_id, puzzles.to_json(), now, now + self.settings.lobby_ttl_seconds),
            )
            size = TEAM_SIZE[mode]
            code_a = None if mode == "1v1" else self._new_code(conn)
            code_b = self._new_code(conn)
            team_a = conn.execute(
                "INSERT INTO teams (match_id, label, join_code, capacity, chat_id) VALUES (?, 'A', ?, ?, ?)",
                (match_id, code_a, size, user["private_chat_id"] if mode == "1v1" else None),
            ).lastrowid
            conn.execute(
                "INSERT INTO teams (match_id, label, join_code, capacity) VALUES (?, 'B', ?, ?)",
                (match_id, code_b, size),
            )
            conn.execute(
                "INSERT INTO team_members (team_id, match_id, user_id, joined_at) VALUES (?, ?, ?, ?)",
                (team_a, match_id, user_id, now),
            )
        log.info("Match %s (%s) created by %s", match_id, mode, user_id)
        return CreatedMatch(match_id, mode, code_a, code_b)

    def _unique(self, conn: sqlite3.Connection, sql: str, length: int) -> str:
        for _ in range(20):
            value = generate_code(length)
            if conn.execute(sql, (value,)).fetchone() is None:
                return value
        raise RuntimeError("Could not generate a unique identifier")

    def _new_code(self, conn: sqlite3.Connection) -> str:
        return self._unique(conn, "SELECT 1 FROM teams WHERE join_code = ?", CODE_LENGTH)

    # -- join ---------------------------------------------------------------
    def join(self, user_id: int, raw_code: str | None, chat_id: int, chat_type: str,
             chat_title: str | None = None, bot_username: str | None = None) -> Outcome:
        self.expire_lobbies()
        now = self.clock()
        if self._too_many_failures(user_id, now):
            return Outcome("⏱️ Too many invalid codes. Please wait a few minutes and try again.")
        code = normalize_code(raw_code)
        if code is None:
            self._record_failure(user_id, now)
            return Outcome("❌ That doesn't look like a valid join code. Codes have 8 letters/digits.")
        try:
            with self.db.transaction() as conn:
                return self._join(conn, user_id, code, chat_id, chat_type, chat_title, bot_username, now)
        except _InvalidCode:
            self._record_failure(user_id, now)
            return Outcome("❌ Invalid or expired join code. Double-check it with the match creator.")
        except GameError as exc:
            return Outcome(exc.message, exc.buttons)

    def _join(self, conn, user_id, code, chat_id, chat_type, chat_title, bot_username, now) -> Outcome:
        team = conn.execute("SELECT * FROM teams WHERE join_code = ?", (code,)).fetchone()
        if team is None:
            raise _InvalidCode()
        match = get_match(conn, team["match_id"])
        if match["status"] != "lobby":
            if match["status"] == "active":
                raise GameError("🚪 That match has already started.")
            raise _InvalidCode()

        user = get_user(conn, user_id)
        if user is None or user["private_chat_id"] is None:
            buttons = None
            if bot_username:
                buttons = [[Button("▶️ Open private chat", url=f"https://t.me/{bot_username}?start=hello")]]
            raise GameError(
                "👤 Before joining, open a private chat with me and press <b>Start</b> once, "
                "then send the join command here again.",
                buttons,
            )

        existing = find_open_participation(conn, user_id)
        if existing and existing["match_id"] != match["match_id"]:
            raise GameError(
                f"🎮 You're already in match <code>{existing['match_id']}</code>. "
                "Finish it or /quit it first."
            )
        if existing and existing["team_id"] != team["team_id"]:
            raise GameError("⛔ You're already registered on the other side of this match.")

        if match["mode"] == "1v1":
            reply = self._join_1v1(conn, match, team, user, chat_id, chat_type, existing, bot_username, now)
        else:
            reply = self._join_2v2(conn, match, team, user, chat_id, chat_type, chat_title,
                                   existing, bot_username, now)

        if self._ready(conn, match["match_id"]):
            self._start(conn, match["match_id"], now)
            reply += "\n\n🚨 Everyone is here - the match is starting!"
        return Outcome(reply, data={"match_id": match["match_id"]})

    def _join_1v1(self, conn, match, team, user, chat_id, chat_type, existing, bot_username, now) -> str:
        if chat_type != "private":
            buttons = None
            if bot_username:
                buttons = [[Button("▶️ Join in private chat", url=f"https://t.me/{bot_username}")]]
            raise GameError("🔒 1v1 codes must be used in a private chat with me.", buttons)
        if existing:
            return "✅ You're already registered in this match."
        if self._member_count(conn, team["team_id"]) >= team["capacity"]:
            raise GameError("🚫 This match is already full.")
        self._add_member(conn, team, user["user_id"], now)
        conn.execute("UPDATE teams SET chat_id = ? WHERE team_id = ?", (chat_id, team["team_id"]))
        creator = get_user(conn, match["creator_id"])
        enqueue_text(
            conn, chat_id=creator["private_chat_id"],
            text=f"🙌 <b>{escape(display_name(user))}</b> joined your 1v1 match "
                 f"<code>{match['match_id']}</code>.",
            dedupe_key=f"joined:{match['match_id']}:{user['user_id']}", now=now, match_id=match["match_id"],
        )
        return (
            f"✅ You joined 1v1 match <code>{match['match_id']}</code> against "
            f"<b>{escape(display_name(creator))}</b>."
        )

    def _join_2v2(self, conn, match, team, user, chat_id, chat_type, chat_title, existing,
                  bot_username, now) -> str:
        label = team["label"]
        if chat_type not in GROUP_CHAT_TYPES:
            buttons = None
            if bot_username:
                buttons = [[Button(f"➕ Add me to Team {label}'s group",
                                   url=f"https://t.me/{bot_username}?startgroup={team['join_code']}")]]
            raise GameError(
                f"👥 This is a <b>Team {label}</b> code for a 2v2 match. Use it inside your team's "
                f"own Telegram group: add me to the group, then send <code>/join {team['join_code']}</code> there.",
                buttons,
            )
        linked = find_linked_team(conn, chat_id)
        if linked is not None and linked["team_id"] != team["team_id"]:
            raise GameError("⛔ This group is already linked to a different team. Each team needs its own group.")
        if team["chat_id"] is not None and team["chat_id"] != chat_id:
            raise GameError(f"⛔ Team {label} is already linked to a different group - join from that group.")

        notes = []
        if linked is None:
            conn.execute(
                """
                INSERT INTO linked_groups (chat_id, team_id, match_id, title, linked_by, linked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, team["team_id"], match["match_id"], (chat_title or "")[:128], user["user_id"], now),
            )
            conn.execute("UPDATE teams SET chat_id = ? WHERE team_id = ?", (chat_id, team["team_id"]))
            notes.append(f"🔗 This group is now <b>Team {label}</b>'s group for match <code>{match['match_id']}</code>.")

        if existing:
            notes.append(f"✅ {escape(display_name(user))} is already on Team {label}.")
        else:
            if self._member_count(conn, team["team_id"]) >= team["capacity"]:
                raise GameError(f"🚫 Team {label} already has two players.")
            self._add_member(conn, team, user["user_id"], now)
            notes.append(f"✅ <b>{escape(display_name(user))}</b> joined <b>Team {label}</b>.")
            creator = get_user(conn, match["creator_id"])
            if creator["user_id"] != user["user_id"]:
                enqueue_text(
                    conn, chat_id=creator["private_chat_id"],
                    text=f"🙌 <b>{escape(display_name(user))}</b> joined Team {label} in match "
                         f"<code>{match['match_id']}</code>.",
                    dedupe_key=f"joined:{match['match_id']}:{user['user_id']}", now=now,
                    match_id=match["match_id"],
                )

        count = self._member_count(conn, team["team_id"])
        if count < team["capacity"]:
            notes.append(
                f"Waiting for your teammate: they should press Start in a private chat with me, "
                f"then send <code>/join {team['join_code']}</code> in this group."
            )
        return "\n".join(notes)

    def _member_count(self, conn, team_id: int) -> int:
        return conn.execute("SELECT COUNT(*) FROM team_members WHERE team_id = ?", (team_id,)).fetchone()[0]

    def _add_member(self, conn, team, user_id: int, now: float) -> None:
        conn.execute(
            "INSERT INTO team_members (team_id, match_id, user_id, joined_at) VALUES (?, ?, ?, ?)",
            (team["team_id"], team["match_id"], user_id, now),
        )

    # -- start --------------------------------------------------------------
    def _ready(self, conn, match_id: str) -> bool:
        for team in get_teams(conn, match_id):
            if team["chat_id"] is None or self._member_count(conn, team["team_id"]) < team["capacity"]:
                return False
        return True

    def _start(self, conn, match_id: str, now: float) -> None:
        cur = conn.execute(
            "UPDATE matches SET status = 'active', started_at = ? WHERE match_id = ? AND status = 'lobby'",
            (now, match_id),
        )
        if cur.rowcount != 1:
            return
        match = get_match(conn, match_id)
        teams = get_teams(conn, match_id)
        for team, opponent in ((teams[0], teams[1]), (teams[1], teams[0])):
            enqueue_text(
                conn,
                chat_id=team["chat_id"],
                text=msg.match_started(match["mode"], side_name(conn, team, match["mode"]),
                                       side_name(conn, opponent, match["mode"]), match_id),
                dedupe_key=f"start:{match_id}:{team['team_id']}",
                now=now, match_id=match_id, kind="start",
            )
            enqueue_room(conn, self.puzzles, match, team, 1, now)
        log.info("Match %s started", match_id)

    # -- lobby housekeeping -----------------------------------------------------
    def expire_lobbies(self) -> list[str]:
        now = self.clock()
        with self.db.transaction() as conn:
            expired = [
                r["match_id"] for r in conn.execute(
                    "SELECT match_id FROM matches WHERE status = 'lobby' AND expires_at < ?", (now,)
                )
            ]
            for match_id in expired:
                conn.execute(
                    "UPDATE matches SET status = 'expired', end_reason = 'expired', ended_at = ? "
                    "WHERE match_id = ?",
                    (now, match_id),
                )
                release_groups(conn, match_id, now)
                creator = conn.execute(
                    "SELECT u.private_chat_id FROM matches m JOIN users u ON u.user_id = m.creator_id "
                    "WHERE m.match_id = ?",
                    (match_id,),
                ).fetchone()
                enqueue_text(
                    conn, chat_id=creator["private_chat_id"],
                    text=f"⌛ Your lobby <code>{match_id}</code> expired before everyone joined. "
                         "Use /create to start a new one.",
                    dedupe_key=f"expired:{match_id}", now=now, match_id=match_id,
                )
        return expired

    def _too_many_failures(self, user_id: int, now: float) -> bool:
        count = self.db.query_one(
            "SELECT COUNT(*) AS n FROM join_failures WHERE user_id = ? AND created_at > ?",
            (user_id, now - JOIN_FAILURE_WINDOW),
        )["n"]
        return count >= self.settings.join_failure_limit

    def _record_failure(self, user_id: int, now: float) -> None:
        self.db.execute("INSERT INTO join_failures (user_id, created_at) VALUES (?, ?)", (user_id, now))


class _InvalidCode(Exception):
    pass

