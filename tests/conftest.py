"""Shared fixtures: a real SQLite database on disk, the real puzzle file and
the real services, with a controllable clock. Nothing here mocks the game
logic; only Telegram itself is replaced by a recording fake bot."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from config import BASE_DIR, Settings
from database import Database
from game_engine import GameEngine
from hint_manager import HintManager
from matchmaking import Matchmaking
from notifications import Notifier
from puzzle_manager import PuzzleProvider
from state import PuzzleCache

PUZZLES_PATH = BASE_DIR / "puzzles" / "puzzles.json"
ASSETS_DIR = BASE_DIR / "assets" / "puzzle_images"
# Gameplay tests run with the first puzzle listed for each room (see the
# autouse fixture below), so these are the answers they play with.
ANSWERS = {}
for _p in json.loads(PUZZLES_PATH.read_text())["puzzles"]:
    ANSWERS.setdefault(_p["room"], _p["accepted_answers"][0])

GROUP_A = -1001
GROUP_B = -1002


def match_answer(db, user_id: int, room: int) -> str:
    """The correct answer for ``room`` in the user's latest match (its saved puzzles)."""
    from puzzle_manager import PuzzleSet
    row = db.query_one(
        "SELECT m.puzzle_snapshot FROM team_members tm JOIN matches m ON m.match_id = tm.match_id "
        "WHERE tm.user_id = ? "
        "ORDER BY m.status IN ('lobby', 'active') DESC, m.created_at DESC, m.rowid DESC LIMIT 1", (user_id,))
    return PuzzleSet.from_json(row["puzzle_snapshot"]).get(room).accepted_answers[0]


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class World:
    path: Path
    clock: FakeClock
    settings: Settings
    db: Database = field(init=False)
    mm: Matchmaking = field(init=False)
    engine: GameEngine = field(init=False)
    hints: HintManager = field(init=False)

    def __post_init__(self):
        self.open()

    def open(self) -> None:
        """(Re)open the database and build fresh services - like a process start."""
        self.db = Database(self.path)
        self.db.migrate()
        provider = PuzzleProvider(PUZZLES_PATH, ASSETS_DIR)
        cache = PuzzleCache()
        self.mm = Matchmaking(self.db, provider.get, self.settings, self.clock, cache)
        self.engine = GameEngine(self.db, self.settings, self.clock, cache)
        self.hints = HintManager(self.db, self.settings, self.clock, cache)

    def restart(self) -> None:
        self.db.close()
        self.open()

    # -- helpers ----------------------------------------------------------
    def user(self, user_id: int, name: str, private: bool = True) -> int:
        self.mm.register_user(user_id, name, None, name.lower(),
                              user_id if private else -999, "private" if private else "group")
        return user_id

    def outbox(self, chat_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM outbox"
        params: tuple = ()
        if chat_id is not None:
            sql += " WHERE chat_id = ?"
            params = (chat_id,)
        rows = self.db.query_all(sql + " ORDER BY message_id", params)
        return [dict(r) | {"text": json.loads(r["payload"]).get("text", "")} for r in rows]

    def texts(self, chat_id: int) -> list[str]:
        return [m["text"] for m in self.outbox(chat_id)]

    def team(self, match_id: str, label: str):
        return self.db.query_one("SELECT * FROM teams WHERE match_id = ? AND label = ?", (match_id, label))

    def match(self, match_id: str):
        return self.db.query_one("SELECT * FROM matches WHERE match_id = ?", (match_id,))

    def setup_1v1(self, a: int = 1, b: int = 2) -> str:
        self.user(a, f"Alice{a}")
        self.user(b, f"Bob{b}")
        created = self.mm.create_match(a, "1v1")
        out = self.mm.join(b, created.code_b, b, "private")
        assert "joined" in out.text, out.text
        return created.match_id

    def setup_2v2(self, ids=(1, 2, 3, 4), groups=(GROUP_A, GROUP_B)) -> str:
        names = ["Ann", "Ben", "Cat", "Dan"]
        for uid, name in zip(ids, names):
            self.user(uid, f"{name}{uid}")
        created = self.mm.create_match(ids[0], "2v2")
        for uid in ids[:2]:
            self.mm.join(uid, created.code_a, groups[0], "supergroup", "Team A group")
        for uid in ids[2:]:
            self.mm.join(uid, created.code_b, groups[1], "supergroup", "Team B group")
        assert self.match(created.match_id)["status"] == "active"
        return created.match_id

    def answer(self, user_id: int, chat_id: int, chat_type: str, guess: str):
        return self.engine.submit_answer(user_id, chat_id, chat_type, guess)

    def answer_for(self, user_id: int, room: int) -> str:
        """The correct answer for ``room`` in the user's current (or last) match."""
        return match_answer(self.db, user_id, room)

    def snapshot_ids(self, match_id: str) -> list[str]:
        from puzzle_manager import PuzzleSet
        return [p.id for p in PuzzleSet.from_json(self.match(match_id)["puzzle_snapshot"])]

    def solve(self, user_id: int, chat_id: int, chat_type: str, rooms) -> None:
        for room in rooms:
            out = self.answer(user_id, chat_id, chat_type, self.answer_for(user_id, room))
            assert out.text is None, f"room {room}: {out.text}"


class FirstChoice:
    """Stand-in for random.SystemRandom that always picks the first option."""

    def choice(self, seq):
        return seq[0]

    def random(self):
        return 0.0  # ties keep file order, so the first unseen option wins


@pytest.fixture(autouse=True)
def deterministic_puzzles(monkeypatch):
    """Matches normally draw random puzzles; tests use a fixed draw."""
    import puzzle_manager
    monkeypatch.setattr(puzzle_manager.secrets, "SystemRandom", FirstChoice)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(database_path=tmp_path / "test.db", puzzles_path=PUZZLES_PATH,
                    assets_dir=ASSETS_DIR, answer_rate_limit_per_minute=1000)


@pytest.fixture
def world(tmp_path, settings) -> World:
    w = World(tmp_path / "test.db", FakeClock(), settings)
    yield w
    w.db.close()


class FakeBot:
    """Records sends; can be told to fail for particular chats."""

    def __init__(self):
        self.sent: list[dict] = []
        self.fail: dict[int, list[Exception]] = {}

    async def _maybe_fail(self, chat_id):
        queue = self.fail.get(chat_id)
        if queue:
            raise queue.pop(0)

    async def send_message(self, chat_id, text, **kwargs):
        await self._maybe_fail(chat_id)
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        await self._maybe_fail(chat_id)
        self.sent.append({"chat_id": chat_id, "photo": getattr(photo, "name", photo), "caption": caption})

    def texts(self, chat_id):
        return [m.get("text") for m in self.sent if m["chat_id"] == chat_id]


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def notifier(world, fake_bot) -> Notifier:
    async def no_sleep(_):
        return None
    return Notifier(world.db, fake_bot, ASSETS_DIR, clock=world.clock, sleep=no_sleep, max_attempts=3)
