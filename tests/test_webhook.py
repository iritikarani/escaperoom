"""Webhook mode (PythonAnywhere): real WSGI app, real handlers, fake Telegram."""

import io
import json

import pytest

import pythonanywhere_setup as setup
import webhook_app
from tests.test_e2e import FakeTelegram, callback, codes_in, message

SECRET = "s3cret-value"


@pytest.fixture
def wsgi(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "hook.db"))
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "")
    monkeypatch.setattr(webhook_app, "_app", None)
    fake = FakeTelegram()
    app = webhook_app._get_app(request=fake)
    yield fake
    webhook_app._loop.run_until_complete(app.shutdown())
    app.bot_data["services"].db.close()


def call(data=None, secret=SECRET, method="POST", path="/telegram", raw=None):
    body = raw if raw is not None else json.dumps(data or {}).encode()
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
    }
    if secret is not None:
        environ["HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"] = secret
    status = {}
    webhook_app.application(environ, lambda s, headers: status.setdefault("s", s))
    return status["s"]


def test_health_and_routing(wsgi):
    assert call(method="GET", path="/") == "200 OK"
    assert call(method="GET", path="/telegram") == "405 Method Not Allowed"
    assert call(path="/elsewhere") == "404 Not Found"


def test_rejects_missing_or_wrong_secret(wsgi):
    update = message(1, "Alice", "/start")
    assert call(update, secret=None) == "403 Forbidden"
    assert call(update, secret="wrong") == "403 Forbidden"
    assert not any(e == "sendMessage" for e, _ in wsgi.calls)


def test_rejects_bad_bodies(wsgi):
    assert call(raw=b"not json") == "400 Bad Request"
    assert call(raw=b"") == "400 Bad Request"


def test_full_1v1_over_webhook(wsgi):
    fake = wsgi
    assert call(message(1, "Alice", "/start")) == "200 OK"
    assert "Welcome" in fake.sent_to(1)
    call(callback(1, "Alice", "create:1v1"))
    code = codes_in(fake.sent_to(1))[0]
    call(message(2, "Bob", f"/start {code}"))
    assert "ROOM 1 / 7" in fake.sent_to(1) and "ROOM 1 / 7" in fake.sent_to(2)
    call(message(1, "Alice", "369"))
    assert "ROOM 2 UNLOCKED" in fake.sent_to(1)
    assert "Your opponent just reached <b>Room 2</b>" in fake.sent_to(2)


def test_duplicate_webhook_delivery_processed_once(wsgi):
    update = message(1, "Alice", "/start")
    call(update)
    call(update)  # Telegram retried
    assert sum("Welcome" in t for t in wsgi.texts(1)) == 1


def test_handler_crash_still_returns_200(wsgi, monkeypatch):
    import bot
    async def boom(update, context):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(bot.Services, "build", None)  # not used after startup
    app = webhook_app._app
    app.handlers[0][0].callback = boom  # /start handler
    assert call(message(1, "Alice", "/start")) == "200 OK"


# -- setup script ---------------------------------------------------------------------

def test_env_roundtrip(tmp_path):
    path = tmp_path / ".env"
    setup.write_env(path, {"A": "1", "TELEGRAM_BOT_TOKEN": "x:y"})
    assert setup.read_env(path) == {"A": "1", "TELEGRAM_BOT_TOKEN": "x:y"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_wsgi_file_imports_application(tmp_path):
    src = setup.wsgi_file_contents(tmp_path)
    assert "from webhook_app import application" in src
    compile(src, "wsgi.py", "exec")


def test_setup_script_writes_env_and_sets_webhook(tmp_path, monkeypatch):
    token = "123456789:" + "A" * 35
    calls = []

    def fake_telegram(tok, method, payload, proxy):
        calls.append((method, payload))
        if method == "getMe":
            return {"ok": True, "result": {"username": "EscapeBot"}}
        return {"ok": True, "result": True}

    monkeypatch.setattr(setup, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(setup, "telegram", fake_telegram)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt="": token)
    monkeypatch.setattr(setup.getpass, "getuser", lambda: "unknowngamer")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    assert setup.main([]) == 0

    env = setup.read_env(tmp_path / ".env")
    assert env["TELEGRAM_BOT_TOKEN"] == token and len(env["WEBHOOK_SECRET"]) >= 32
    hook = dict(calls)["setWebhook"]
    assert hook["url"] == "https://unknowngamer.pythonanywhere.com/telegram"
    assert hook["secret_token"] == env["WEBHOOK_SECRET"]
    assert (tmp_path / "x.db").exists()

    # Re-running keeps the same secret and doesn't ask for the token again
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt="": pytest.fail("asked again"))
    assert setup.main([]) == 0
    assert setup.read_env(tmp_path / ".env")["WEBHOOK_SECRET"] == env["WEBHOOK_SECRET"]


def test_setup_script_rejects_bad_token(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt="": "not-a-token")
    with pytest.raises(SystemExit):
        setup.main([])


async def test_maintenance_retries_pending_and_expires_lobbies(tmp_path):
    import bot
    from config import Settings
    settings = Settings(bot_token="123456:TEST", database_path=tmp_path / "m.db", lobby_ttl_seconds=0)
    svc = bot.Services.build(settings)
    svc.matchmaking.register_user(1, "Alice", None, None, 1, "private")
    created = svc.matchmaking.create_match(1, "1v1")
    fake = FakeTelegram()
    await bot.run_maintenance(svc, request=fake)
    status = svc.db.query_one("SELECT status FROM matches WHERE match_id = ?", (created.match_id,))
    assert status["status"] == "expired"
    assert "expired" in fake.sent_to(1)
    assert svc.db.query_one("SELECT COUNT(*) AS n FROM outbox WHERE status = 'pending'")["n"] == 0
    svc.db.close()
