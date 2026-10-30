"""Shared game-state lookups used by matchmaking, the engine and hints.

All functions take an open ``sqlite3.Connection`` so callers can compose them
inside a single transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from puzzle_manager import PuzzleSet

OPEN_STATUSES = ("lobby", "active")
GROUP_CHAT_TYPES = ("group", "supergroup")
# Pseudo chat type for actions coming from the Mini App: the side is the
# user's own match, and in 2v2 the action is echoed to the team group.
MINIAPP = "miniapp"


@dataclass(frozen=True)
class Button:
    """A Telegram-agnostic inline button (callback data, URL or Mini App URL)."""

    label: str
    callback: str | None = None
    url: str | None = None
    web_app: str | None = None


class GameError(Exception):
    """An expected, user-facing refusal. The message is safe to show."""

    def __init__(self, message: str, buttons: list[list[Button]] | None = None):
        super().__init__(message)
        self.message = message
        self.buttons = buttons


@dataclass
class SideContext:
    match: sqlite3.Row
    team: sqlite3.Row
    opponent: sqlite3.Row
    user_id: int
    via: str = "chat"

    @property
    def mode(self) -> str:
        return self.match["mode"]


# -- lookups ----------------------------------------------------------------

def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def get_match(conn: sqlite3.Connection, match_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()


def get_team(conn: sqlite3.Connection, team_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM teams WHERE team_id = ?", (team_id,)).fetchone()


def get_teams(conn: sqlite3.Connection, match_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM teams WHERE match_id = ? ORDER BY label", (match_id,)
    ).fetchall()


def get_opponent(conn: sqlite3.Connection, team: sqlite3.Row) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM teams WHERE match_id = ? AND team_id != ?",
        (team["match_id"], team["team_id"]),
    ).fetchone()


def get_members(conn: sqlite3.Connection, team_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT u.* FROM team_members m JOIN users u ON u.user_id = m.user_id
        WHERE m.team_id = ? ORDER BY m.joined_at, u.user_id
        """,
        (team_id,),
    ).fetchall()


def find_open_participation(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """The lobby/active match a user is registered in, if any."""
    return conn.execute(
        """
        SELECT m.match_id, m.mode, m.status, m.creator_id, tm.team_id
        FROM team_members tm JOIN matches m ON m.match_id = tm.match_id
        WHERE tm.user_id = ? AND m.status IN ('lobby', 'active')
        ORDER BY m.created_at DESC LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def find_last_participation(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """The user's most recent finished match (so /status can show the result)."""
    return conn.execute(
        """
        SELECT m.match_id, m.mode, m.status, m.creator_id, tm.team_id
        FROM team_members tm JOIN matches m ON m.match_id = tm.match_id
        WHERE tm.user_id = ? AND m.status = 'completed'
        ORDER BY m.ended_at DESC LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def find_last_linked_team(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    """The team a group served most recently, after its match finished."""
    return conn.execute(
        """
        SELECT t.* FROM linked_groups g
        JOIN teams t ON t.team_id = g.team_id
        JOIN matches m ON m.match_id = g.match_id
        WHERE g.chat_id = ? AND m.status = 'completed'
        ORDER BY g.released_at DESC LIMIT 1
        """,
        (chat_id,),
    ).fetchone()


def find_linked_team(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT t.* FROM linked_groups g JOIN teams t ON t.team_id = g.team_id
        WHERE g.chat_id = ? AND g.released_at IS NULL
        """,
        (chat_id,),
    ).fetchone()


def is_member(conn: sqlite3.Connection, team_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_id)
    ).fetchone() is not None


def release_groups(conn: sqlite3.Connection, match_id: str, now: float) -> None:
    conn.execute(
        "UPDATE linked_groups SET released_at = ? WHERE match_id = ? AND released_at IS NULL",
        (now, match_id),
    )


def migrate_chat_id(conn: sqlite3.Connection, old_chat_id: int, new_chat_id: int) -> None:
    """Telegram changes a group's id when it is upgraded to a supergroup."""
    conn.execute("UPDATE teams SET chat_id = ? WHERE chat_id = ?", (new_chat_id, old_chat_id))
    conn.execute(
        "UPDATE linked_groups SET chat_id = ? WHERE chat_id = ? AND released_at IS NULL",
        (new_chat_id, old_chat_id),
    )
    conn.execute(
        "UPDATE outbox SET chat_id = ? WHERE chat_id = ? AND status = 'pending'",
        (new_chat_id, old_chat_id),
    )


# -- display ----------------------------------------------------------------

def display_name(user: sqlite3.Row) -> str:
    name = " ".join(p for p in (user["first_name"], user["last_name"]) if p).strip()
    if name:
        return name
    if user["username"]:
        return f"@{user['username']}"
    return "Player"


def side_name(conn: sqlite3.Connection, team: sqlite3.Row, mode: str) -> str:
    members = [display_name(u) for u in get_members(conn, team["team_id"])]
    if mode == "1v1":
        return members[0] if members else "Player"
    roster = " & ".join(members) if members else "waiting for players"
    return f"Team {team['label']} ({roster})"


# -- context resolution -----------------------------------------------------

def resolve_side(
    conn: sqlite3.Connection,
    user_id: int,
    chat_id: int,
    chat_type: str,
    *,
    require_team_chat: bool,
) -> SideContext:
    """Work out which match and side an incoming action belongs to.

    * Private chat: the user's own open match. In 2v2, actions that teammates
      must see (answers, hints) are refused here and redirected to the group.
    * Group chat: the team this group is linked to, and only if the sender is
      a registered member of that team. This is what stops an unrelated group
      or an outsider from controlling a team's game.
    """
    if chat_type in ("private", MINIAPP):
        part = find_open_participation(conn, user_id) or find_last_participation(conn, user_id)
        if part is None:
            raise GameError(
                "🙈 You're not in a match right now. Use /create to start one or "
                "/join CODE to join one."
            )
        if part["mode"] == "2v2" and require_team_chat and chat_type == "private":
            raise GameError(
                "👥 This is a 2v2 match - please do that in your team group, so your "
                "teammate sees it too."
            )
        team = get_team(conn, part["team_id"])
    elif chat_type in GROUP_CHAT_TYPES:
        team = find_linked_team(conn, chat_id) or find_last_linked_team(conn, chat_id)
        if team is None:
            raise GameError(
                "🔗 This group isn't linked to a match. Send /join CODE here with your team code."
            )
        if not is_member(conn, team["team_id"], user_id):
            raise GameError("⛔ Only the registered members of this team can do that.")
    else:
        raise GameError("This chat type isn't supported.")

    match = get_match(conn, team["match_id"])
    return SideContext(match=match, team=team, opponent=get_opponent(conn, team), user_id=user_id,
                       via=MINIAPP if chat_type == MINIAPP else "chat")


class PuzzleCache:
    """Parsed per-match puzzle snapshots (cache only; the DB is authoritative)."""

    def __init__(self, max_size: int = 256):
        self._cache: dict[str, PuzzleSet] = {}
        self._max = max_size

    def forget(self, match_id: str) -> None:
        self._cache.pop(match_id, None)

    def for_match(self, match: sqlite3.Row) -> PuzzleSet:
        key = match["match_id"]
        puzzles = self._cache.get(key)
        if puzzles is None:
            if len(self._cache) >= self._max:
                self._cache.pop(next(iter(self._cache)))
            puzzles = PuzzleSet.from_json(match["puzzle_snapshot"])
            self._cache[key] = puzzles
        return puzzles


Clock = Callable[[], float]
