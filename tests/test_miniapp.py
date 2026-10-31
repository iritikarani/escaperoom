"""Mini App (visual room screen): signed-login checks and the room API, run
through the real WSGI app and handlers with a fake Telegram API."""

import hashlib
import hmac
import io
import json
import time
from urllib.parse import urlencode

import pytest

import webhook_app
from miniapp import scene_for, verify_init_data
from tests.conftest import match_answer
from tests.test_e2e import FakeTelegram, callback, codes_in, message

TOKEN = "123456:TEST"


def signed(user_id, name="Alice", token=TOKEN, auth_date=None, extra=None):
    fields = {"auth_date": str(int(auth_date or time.time())), "query_id": "AAE",
              "user": json.dumps({"id": user_id, "first_name": name})}
    fields.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


# -- signature checks ---------------------------------------------------------------

def test_valid_init_data_accepted():
    assert verify_init_data(signed(42), TOKEN)["id"] == 42


def test_tampered_init_data_rejected():
    data = signed(42).replace("42", "43")
    assert verify_init_data(data, TOKEN) is None


def test_wrong_bot_token_rejected():
    assert verify_init_data(signed(42, token="999:OTHER"), TOKEN) is None


def test_expired_init_data_rejected():
    assert verify_init_data(signed(42, auth_date=time.time() - 3 * 24 * 3600), TOKEN) is None


@pytest.mark.parametrize("bad", ["", "garbage", "hash=abc", "user=%7B%7D&auth_date=1&hash=00"])
def test_malformed_init_data_rejected(bad):
    assert verify_init_data(bad, TOKEN) is None


def test_current_clients_with_signature_field_accepted():
    # Current Telegram apps send `signature` and include it in the hash.
    data = signed(7, extra={"signature": "Gx8QzS0bD1x-sAmPlE_sig"})
    assert verify_init_data(data, TOKEN)["id"] == 7


def test_older_clients_without_signature_accepted():
    assert verify_init_data(signed(7), TOKEN)["id"] == 7


def test_signature_field_cannot_be_tampered():
    data = signed(7, extra={"signature": "original"})
    assert verify_init_data(data.replace("original", "changed"), TOKEN) is None


def test_scene_mapping():
    assert scene_for("padlock-triangle") == "lock"
    assert scene_for("binary-mirror") == "lamps"
    assert scene_for("phone-keypad-compass") == "phone"
    assert scene_for("something-new") == "lock"


# -- API through the WSGI app ---------------------------------------------------------

@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("WEBHOOK_SECRET", "hook")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "")
    monkeypatch.setenv("WEBAPP_URL", "https://example.test/app")
    monkeypatch.setenv("MINIAPP_LINK", "https://t.me/EscapeBot/room")
    monkeypatch.setattr(webhook_app, "_app", None)
    fake = FakeTelegram()
    app = webhook_app._get_app(request=fake)
    yield fake
    webhook_app._loop.run_until_complete(app.shutdown())
    app.bot_data["services"].db.close()


def request(method, path, body=None, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)),
               "wsgi.input": io.BytesIO(raw)}
    environ.update(headers or {})
    out = {}
    chunks = webhook_app.application(environ, lambda s, h: out.update(status=s, headers=dict(h)))
    out["body"] = b"".join(chunks)
    return out


def hook(update):
    return request("POST", "/telegram", update, {"HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "hook"})


def answer(user_id, room):
    return match_answer(webhook_app._app.bot_data["services"].db, user_id, room)


def api(action, user_id, name="Alice", **extra):
    res = request("POST", f"/api/{action}", {"initData": signed(user_id, name), **extra})
    return res["status"], json.loads(res["body"])


def start_1v1(fake):
    hook(message(1, "Alice", "/start"))
    hook(callback(1, "Alice", "create:1v1"))
    code = codes_in(fake.sent_to(1))[0]
    hook(message(2, "Bob", f"/start {code}"))


def start_2v2(fake):
    for uid, name in ((1, "Ann"), (2, "Ben"), (3, "Cat"), (4, "Dan")):
        hook(message(uid, name, "/start"))
    hook(callback(1, "Ann", "create:2v2"))
    a, b = codes_in(fake.sent_to(1))[:2]
    hook(message(1, "Ann", f"/join {a}", chat_id=-101, chat_type="supergroup"))
    hook(message(2, "Ben", f"/join {a}", chat_id=-101, chat_type="supergroup"))
    hook(message(3, "Cat", f"/join {b}", chat_id=-102, chat_type="supergroup"))
    hook(message(4, "Dan", f"/join {b}", chat_id=-102, chat_type="supergroup"))


def test_page_is_served(site):
    res = request("GET", "/app")
    assert res["status"] == "200 OK" and res["headers"]["Content-Type"].startswith("text/html")
    assert b"telegram-web-app.js" in res["body"] and b"/api/" in res["body"]


def test_api_requires_valid_signature(site):
    assert request("POST", "/api/state", {"initData": "nope"})["status"] == "401 Unauthorized"
    assert request("POST", "/api/state", {"initData": signed(1, token="9:X")})["status"] == "401 Unauthorized"
    assert request("POST", "/api/state", b"not json")["status"] == "400 Bad Request"
    assert request("GET", "/api/state")["status"] == "405 Method Not Allowed"


def test_image_route_blocks_traversal(site):
    assert request("GET", "/app/img/../../config.py")["status"] == "404 Not Found"
    assert request("GET", "/app/img/missing.png")["status"] == "404 Not Found"


def test_not_in_a_match(site):
    status, body = api("state", 99, "Nobody")
    assert status == "200 OK" and body["state"] == {"status": "none"}


def test_lobby_state(site):
    hook(message(1, "Alice", "/start"))
    hook(callback(1, "Alice", "create:1v1"))
    state = api("state", 1)[1]["state"]
    assert state["status"] == "lobby" and state["lobby"][0]["players"] == ["Alice"]


def test_play_a_room_from_the_app(site):
    fake = site
    start_1v1(fake)
    state = api("state", 1)[1]["state"]
    assert state["status"] == "active" and state["puzzle"]["room"] == 1
    assert state["puzzle"]["scene"] == "lock" and state["puzzle"]["hints"] == []
    assert state["you"]["paid_hints_left"] == 3

    body = api("answer", 1, guess="000")[1]
    assert body["correct"] is False and body["message"]
    assert body["state"]["you"]["room"] == 1

    body = api("answer", 1, guess=answer(1, 1))[1]
    assert body["correct"] is True and body["state"]["puzzle"]["room"] == 2
    # Chat side effects still happen: opponent alert and the next room in chat
    assert "Your opponent just reached <b>Room 2</b>" in fake.sent_to(2)
    assert "ROOM 2 / 7" in fake.sent_to(1)
    assert api("state", 2, "Bob")[1]["state"]["opponent"]["room"] == 2


def test_hint_from_the_app_gives_opponent_free_hint(site):
    fake = site
    start_1v1(fake)
    body = api("hint", 1)[1]
    assert body["ok"] and len(body["state"]["puzzle"]["hints"]) == 1
    assert body["state"]["you"]["paid_hints_left"] == 2
    assert "FREE HINT" in fake.sent_to(2)
    bob = api("state", 2, "Bob")[1]["state"]
    assert len(bob["puzzle"]["hints"]) == 1 and bob["you"]["free_hints"] == 1


def test_state_never_leaks_opponent_details(site):
    start_1v1(site)
    api("answer", 1, guess=answer(1, 1))
    bob = api("state", 2, "Bob")[1]["state"]
    assert set(bob["opponent"]) == {"name", "room"}
    assert bob["puzzle"]["room"] == 1  # Bob still sees his own room only
    assert "accepted_answers" not in json.dumps(bob) and "solution" not in json.dumps(bob)
    assert answer(2, 1) not in json.dumps(bob["puzzle"])


def test_2v2_app_guesses_are_shown_to_the_team_group(site):
    fake = site
    start_2v2(fake)
    api("answer", 2, "Ben", guess="wrong idea")
    assert "Ben</b> tried <code>wrong idea</code>" in fake.sent_to(-101)
    assert "wrong idea" not in fake.sent_to(-102)
    api("answer", 1, "Ann", guess=answer(1, 1))
    assert "✅ Correct!" in fake.sent_to(-101) and "ROOM 2 UNLOCKED" in fake.sent_to(-101)
    assert api("state", 2, "Ben")[1]["state"]["you"]["room"] == 2  # shared team progress


def test_finished_match_shows_result(site):
    start_1v1(site)
    for room in range(1, 8):
        api("answer", 1, guess=answer(1, room))
    assert api("state", 1)[1]["state"]["result"] == {"won": True, "reason": "escaped"}
    assert api("state", 2, "Bob")[1]["state"]["result"]["won"] is False


def test_room_messages_carry_enter_the_room_buttons(site):
    fake = site
    start_2v2(fake)
    hook(message(5, "Eve", "/start"))
    hook(callback(5, "Eve", "create:1v1"))
    hook(message(6, "Fay", "/start " + codes_in(fake.sent_to(5))[0]))
    rooms = [p for e, p in fake.calls if e == "sendMessage" and "ROOM 1 / 7" in p.get("text", "")]
    markups = {int(p["chat_id"]): json.loads(p["reply_markup"]) if isinstance(p["reply_markup"], str)
               else p["reply_markup"] for p in rooms}
    assert markups[5]["inline_keyboard"][0][0]["web_app"]["url"] == "https://example.test/app"
    assert markups[-101]["inline_keyboard"][0][0]["url"] == "https://t.me/EscapeBot/room"
    welcome = [p for e, p in fake.calls if e == "sendMessage" and "Welcome" in p.get("text", "")][0]
    menu = welcome["reply_markup"] if isinstance(welcome["reply_markup"], dict) else json.loads(welcome["reply_markup"])
    assert menu["inline_keyboard"][0][0]["web_app"]["url"] == "https://example.test/app"


def test_chat_gets_short_room_ready_message_not_the_puzzle(site):
    fake = site
    start_1v1(fake)
    room = [t for t in fake.texts(1) if "ROOM 1 / 7" in t][0]
    assert "waiting inside" in room and "Enter the room" in room
    question = api("state", 1)[1]["state"]["puzzle"]["question"]
    first_line = question.split("\n")[0]
    assert first_line and first_line not in room  # the puzzle text only appears in the room screen
    api("answer", 1, guess=answer(1, 1))
    nxt = [t for t in fake.texts(1) if "ROOM 2 / 7" in t][0]
    assert "ROOM 2 UNLOCKED" in nxt and "waiting inside" in nxt


def test_typed_answers_in_chat_still_work_with_the_room_screen(site):
    fake = site
    start_1v1(fake)
    hook(message(1, "Alice", answer(1, 1)))
    assert api("state", 1)[1]["state"]["you"]["room"] == 2


def test_groups_without_a_room_link_still_get_the_full_puzzle(site):
    settings = webhook_app._app.bot_data["services"].settings
    object.__setattr__(settings, "miniapp_link", "")  # this test's app only
    fake = site
    start_2v2(fake)
    group_room = [t for t in fake.texts(-101) if "ROOM 1 / 7" in t][0]
    assert "Your puzzle:" in group_room and "waiting inside" not in group_room
