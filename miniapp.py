"""Backend for the Telegram Mini App (the visual "room" screen).

The page (``miniapp/index.html``) calls three JSON endpoints, all served by
``webhook_app.py``:

* ``POST /api/state``  - what the player should see right now
* ``POST /api/answer`` - submit a guess (``{"guess": "..."}``)
* ``POST /api/hint``   - use a paid hint

Every request carries Telegram's signed ``initData``. It is verified with the
bot token as described in Telegram's Mini App docs, so the user id can't be
forged and nobody can see another side's puzzles. Game rules are not
duplicated here: answers and hints go through the same engine as chat
commands, so free hints, notifications and winner detection all still apply.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from config import ESCAPED_ROOM, PAID_HINTS_PER_SIDE, TOTAL_ROOMS
from hint_manager import hints_revealed
from state import (
    MINIAPP,
    display_name,
    find_last_participation,
    find_open_participation,
    get_match,
    get_members,
    get_opponent,
    get_team,
    get_teams,
    side_name,
)

INIT_DATA_MAX_AGE = 24 * 3600

# Which object the room screen draws, picked from the puzzle id.
SCENES = [
    ("binary", "lamps"), ("phone", "phone"), ("clock", "clock"), ("rain", "window"),
    ("book", "book"), ("ledger", "ledger"), ("caesar", "scroll"), ("atbash", "scroll"),
    ("shift", "scroll"), ("chest", "chest"), ("boxes", "chest"), ("hooks", "hooks"),
    ("letters", "letters"), ("keypad", "keypad"), ("padlock", "lock"), ("bike", "lock"),
    ("word", "sign"), ("anagram", "sign"), ("compound", "sign"),
]


def scene_for(puzzle_id: str) -> str:
    for keyword, scene in SCENES:
        if keyword in puzzle_id:
            return scene
    return "lock"


def verify_init_data(init_data: str, bot_token: str, now: float | None = None,
                     max_age: int = INIT_DATA_MAX_AGE) -> dict | None:
    """Return the Telegram user dict if ``init_data`` is genuine and fresh."""
    if not init_data or not bot_token:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received_hash = fields.pop("hash", "")
    if not received_hash:
        return None
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()

    def matches(items: dict) -> bool:
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(items.items()))
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, received_hash)

    # Telegram's hash covers every field except `hash` itself - including the
    # newer `signature` field. Older clients send no `signature`; checking a
    # variant without it too keeps them working. Both need the bot token.
    without_signature = {k: v for k, v in fields.items() if k != "signature"}
    if not (matches(fields) or ("signature" in fields and matches(without_signature))):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
        user = json.loads(fields.get("user", "null"))
    except ValueError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    if (now or time.time()) - auth_date > max_age:
        return None
    return user


class MiniAppAPI:
    def __init__(self, services):
        self.svc = services

    # -- actions --------------------------------------------------------------
    def answer(self, user_id: int, guess: str) -> dict:
        outcome = self.svc.engine.submit_answer(user_id, 0, MINIAPP, guess)
        solved = outcome.text is None
        return {"ok": True, "correct": solved, "message": None if solved else outcome.text,
                "state": self.state(user_id)}

    def hint(self, user_id: int) -> dict:
        outcome = self.svc.hints.request_hint(user_id, 0, MINIAPP)
        return {"ok": outcome.text is None, "message": outcome.text, "state": self.state(user_id)}

    # -- state -----------------------------------------------------------------
    def state(self, user_id: int) -> dict:
        db = self.svc.db
        with db.transaction() as conn:
            part = find_open_participation(conn, user_id) or find_last_participation(conn, user_id)
            if part is None:
                return {"status": "none"}
            match = get_match(conn, part["match_id"])
            team = get_team(conn, part["team_id"])
            opponent = get_opponent(conn, team)
            mode = match["mode"]
            data = {
                "status": match["status"],
                "match_id": match["match_id"],
                "mode": mode,
                "total_rooms": TOTAL_ROOMS,
                "you": {
                    "name": side_name(conn, team, mode),
                    "room": team["current_room"],
                    "paid_hints_left": PAID_HINTS_PER_SIDE - team["paid_hints_used"],
                    "free_hints": team["free_hints_received"],
                },
                "opponent": {"name": side_name(conn, opponent, mode), "room": opponent["current_room"]},
            }

            if match["status"] == "lobby":
                data["lobby"] = [
                    {"label": (f"Team {t['label']}" if mode == "2v2" else f"Player {1 if t['label'] == 'A' else 2}"),
                     "players": [display_name(u) for u in get_members(conn, t["team_id"])],
                     "needed": t["capacity"]}
                    for t in get_teams(conn, match["match_id"])
                ]
                return data

            end = match["ended_at"] or self.svc.engine.clock()
            data["elapsed"] = round(end - match["started_at"])

            if match["status"] == "completed":
                won = match["winner_team_id"] == team["team_id"]
                data["result"] = {"won": won, "reason": match["end_reason"]}

            room = team["current_room"]
            if match["status"] == "active" and room < ESCAPED_ROOM:
                puzzle = self.svc.engine.puzzles.for_match(match).get(room)
                revealed = hints_revealed(conn, team["team_id"], room)
                data["puzzle"] = {
                    "room": room,
                    "title": puzzle.title,
                    "difficulty": puzzle.difficulty,
                    "question": puzzle.question,
                    "image": f"/app/img/{puzzle.image}" if puzzle.image else None,
                    "image_caption": puzzle.image_caption,
                    "scene": scene_for(puzzle.id),
                    "hints": list(puzzle.hints[:revealed]),
                    "hints_total": len(puzzle.hints),
                }
            return data
