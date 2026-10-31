"""End-to-end: real python-telegram-bot Application, real handlers, real
database - only Telegram's HTTP API is replaced by an in-process fake."""

import json
import re
from itertools import count

import pytest
from telegram import Update
from telegram.request import BaseRequest

import bot
from config import Settings
from tests.conftest import ASSETS_DIR, PUZZLES_PATH, match_answer

BOT_ID = 999


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self._ids = count(5000)

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 5.0

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {"id": BOT_ID, "is_bot": True, "first_name": "Escape", "username": "EscapeBot"}
        elif endpoint in ("sendMessage", "editMessageText", "sendPhoto"):
            chat_id = int(params.get("chat_id", 1))
            result = {"message_id": next(self._ids), "date": 0,
                      "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                      "from": {"id": BOT_ID, "is_bot": True, "first_name": "Escape"},
                      "text": params.get("text", "")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def texts(self, chat_id):
        return [p.get("text", "") for e, p in self.calls
                if e in ("sendMessage", "editMessageText") and int(p.get("chat_id", 0)) == chat_id]

    def sent_to(self, chat_id):
        return "\n".join(self.texts(chat_id))


_update_ids = count(1)


def message(user_id, name, text, chat_id=None, chat_type="private", reply_to=None):
    chat_id = chat_id or user_id
    msg = {
        "message_id": next(_update_ids), "date": 0, "text": text,
        "chat": {"id": chat_id, "type": chat_type, **({"title": "Team"} if chat_type != "private" else {})},
        "from": {"id": user_id, "is_bot": False, "first_name": name},
    }
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if reply_to:
        msg["reply_to_message"] = reply_to
    return {"update_id": next(_update_ids), "message": msg}


def callback(user_id, name, data, chat_id=None):
    chat_id = chat_id or user_id
    return {"update_id": next(_update_ids), "callback_query": {
        "id": str(next(_update_ids)), "chat_instance": "x", "data": data,
        "from": {"id": user_id, "is_bot": False, "first_name": name},
        "message": {"message_id": 1, "date": 0, "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": BOT_ID, "is_bot": True, "first_name": "Escape"}, "text": "menu"},
    }}


@pytest.fixture
async def app(tmp_path):
    fake = FakeTelegram()
    settings = Settings(bot_token="123456:TEST", database_path=tmp_path / "e2e.db",
                        puzzles_path=PUZZLES_PATH, assets_dir=ASSETS_DIR, outbox_poll_seconds=3600)
    svc = bot.Services.build(settings)
    application = bot.build_application(svc, request=fake)
    await application.initialize()
    await bot.on_startup(application)

    class Harness:
        pass
    h = Harness()
    h.fake, h.bot_data = fake, application.bot_data

    async def feed(data):
        await application.process_update(Update.de_json(data, application.bot))
    h.feed = feed
    yield h
    # No handler may have crashed into the generic error handler.
    assert not any("Something went wrong" in p.get("text", "") for _, p in fake.calls)
    await bot.on_shutdown(application)
    await application.shutdown()


def codes_in(text):
    return re.findall(r"<code>(?:/join )?([A-Z2-9]{8})</code>", text)


async def test_full_1v1_flow(app):
    fake = app.fake
    await app.feed(message(1, "Alice", "/start"))
    assert "Welcome" in fake.sent_to(1)
    await app.feed(callback(1, "Alice", "create:1v1"))
    code = codes_in(fake.sent_to(1))[0]

    await app.feed(message(2, "Bob", f"/start {code}"))   # invite deep link
    for chat in (1, 2):
        assert "THE MATCH HAS BEGUN" in fake.sent_to(chat)
        assert "ROOM 1 / 7" in fake.sent_to(chat)

    await app.feed(message(1, "Alice", "/hint"))            # command, not an answer
    assert "Hint 1/3 for Room 1" in fake.sent_to(1)
    assert "FREE HINT" in fake.sent_to(2)

    db = app.bot_data["services"].db
    await app.feed(message(1, "Alice", f"  {match_answer(db, 1, 1)} "))  # plain-text answer in 1v1
    assert "ROOM 2 UNLOCKED" in fake.sent_to(1)
    assert "Your opponent just reached <b>Room 2</b>" in fake.sent_to(2)
    assert "ROOM 2 / 7" not in fake.sent_to(2)

    await app.feed(message(2, "Bob", "/answer nope"))
    assert any("Not quite" in t or "budge" in t or "Nope" in t for t in fake.texts(2))

    await app.feed(message(2, "Bob", "/status"))
    assert "Opponent room: <b>Room 2 / 7</b>" in fake.texts(2)[-1]

    for room in range(2, 8):
        await app.feed(message(1, "Alice", f"/answer {match_answer(db, 1, room)}"))
    assert "ESCAPE SUCCESSFUL" in fake.sent_to(1)
    assert "MATCH OVER" in fake.sent_to(2)


async def test_duplicate_updates_processed_once(app):
    fake = app.fake
    await app.feed(message(1, "Alice", "/start"))
    await app.feed(callback(1, "Alice", "create:1v1"))
    code = codes_in(fake.sent_to(1))[0]
    await app.feed(message(2, "Bob", f"/join {code}"))
    update = message(1, "Alice", match_answer(app.bot_data["services"].db, 1, 1))
    await app.feed(update)
    await app.feed(update)   # Telegram re-delivers the same update
    svc = app.bot_data["services"]
    assert svc.db.query_one("SELECT COUNT(*) AS n FROM guess_attempts")["n"] == 1


async def test_2v2_flow_with_groups(app):
    fake = app.fake
    for uid, name in ((1, "Ann"), (2, "Ben"), (3, "Cat"), (4, "Dan")):
        await app.feed(message(uid, name, "/start"))
    await app.feed(callback(1, "Ann", "create:2v2"))
    code_a, code_b = codes_in(fake.sent_to(1))[:2]

    # startgroup deep link delivers "/start@EscapeBot CODE" in the group
    await app.feed(message(1, "Ann", f"/start@EscapeBot {code_a}", chat_id=-101, chat_type="supergroup"))
    await app.feed(message(2, "Ben", f"/join {code_a}", chat_id=-101, chat_type="supergroup"))
    await app.feed(message(3, "Cat", f"/join {code_b}", chat_id=-102, chat_type="supergroup"))
    assert "ROOM 1 / 7" not in fake.sent_to(-101)
    await app.feed(message(4, "Dan", f"/join@EscapeBot {code_b}", chat_id=-102, chat_type="supergroup"))
    assert "ROOM 1 / 7" in fake.sent_to(-101) and "ROOM 1 / 7" in fake.sent_to(-102)

    # Ordinary group chat is never an answer
    before = len(fake.calls)
    room1 = match_answer(app.bot_data["services"].db, 2, 1)
    await app.feed(message(2, "Ben", room1, chat_id=-101, chat_type="supergroup"))
    assert len(fake.calls) == before

    await app.feed(message(2, "Ben", f"/answer {room1}", chat_id=-101, chat_type="supergroup"))
    assert "ROOM 2 UNLOCKED" in fake.sent_to(-101) and "solved by Ben" in fake.sent_to(-101)
    assert "opposing team just reached <b>Room 2</b>" in fake.sent_to(-102)

    # The other team can't act in Team A's group
    await app.feed(message(3, "Cat", "/hint", chat_id=-101, chat_type="supergroup"))
    assert "Only the registered members" in fake.texts(-101)[-1]

    # Forfeit needs confirmation from the same user
    await app.feed(message(4, "Dan", "/quit", chat_id=-102, chat_type="supergroup"))
    quit_call = [p for e, p in fake.calls if e == "sendMessage" and "Forfeit" in p.get("text", "")][-1]
    data = json.loads(quit_call["reply_markup"])["inline_keyboard"][0][0]["callback_data"] \
        if isinstance(quit_call["reply_markup"], str) else quit_call["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    await app.feed(callback(3, "Cat", data, chat_id=-102))      # wrong user: refused
    svc = app.bot_data["services"]
    assert svc.db.query_one("SELECT status FROM matches")["status"] == "active"
    await app.feed(callback(4, "Dan", data, chat_id=-102))
    assert svc.db.query_one("SELECT end_reason FROM matches")["end_reason"] == "forfeit"
    assert "YOUR OPPONENT FORFEITED" in fake.sent_to(-101)
    assert "whole team forfeits" in fake.sent_to(-102)
