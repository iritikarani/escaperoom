"""WSGI entry point for webhook hosting, e.g. PythonAnywhere's free plan.

Free web hosts often can't run a program 24/7, so instead of polling
Telegram, Telegram POSTs every update to ``/telegram`` on this app. Each
request is handled to completion before the response is returned.

The python-telegram-bot ``Application`` is created once per worker process
and driven by one dedicated asyncio event loop, so no threads are needed
(PythonAnywhere web apps don't support them).

Without a background loop, upkeep runs during requests instead: every
update retries undelivered messages and expires stale lobbies. For quiet
periods, schedule ``python bot.py --maintenance`` as a daily task.

Requests are only accepted when Telegram sends the secret configured in
``WEBHOOK_SECRET`` (header ``X-Telegram-Bot-Api-Secret-Token``).

The same app serves the Mini App: the room page at ``/app`` and its JSON API
at ``/api/*`` (see ``miniapp.py``), authenticated by Telegram's signed
``initData``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import mimetypes
from pathlib import Path

from telegram import Update
from telegram.ext import Application

import bot
from config import BASE_DIR, load_settings
from miniapp import MiniAppAPI, verify_init_data

log = logging.getLogger("escape_room.webhook")

WEBHOOK_PATH = "/telegram"
MAX_BODY_BYTES = 1_000_000
MINIAPP_PAGE = BASE_DIR / "miniapp" / "index.html"

_loop = asyncio.new_event_loop()
_app: Application | None = None


def _get_app(request=None) -> Application:
    """Build, initialise and recover the bot on first use in this process."""
    global _app
    if _app is None:
        settings = load_settings()
        logging.basicConfig(level=settings.log_level,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        services = bot.Services.build(settings)
        app = bot.build_application(services, request=request)
        _loop.run_until_complete(app.initialize())
        _loop.run_until_complete(bot.prepare(app, register_commands=False))
        _app = app
    return _app


async def _handle(app: Application, update: Update) -> None:
    await app.process_update(update)
    svc: bot.Services = app.bot_data["services"]
    svc.matchmaking.expire_lobbies()
    await svc.notifier.flush()


def _respond(start_response, status: str, body: bytes = b"",
             content_type: str = "text/plain; charset=utf-8", cache: str = "no-store") -> list[bytes]:
    start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(body))),
                            ("Cache-Control", cache), ("X-Content-Type-Options", "nosniff")])
    return [body]


def _json(start_response, status: str, data: dict) -> list[bytes]:
    return _respond(start_response, status, json.dumps(data).encode(), "application/json")


def _read_body(environ) -> bytes | None:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None
    if length <= 0 or length > MAX_BODY_BYTES:
        return None
    return environ["wsgi.input"].read(length)


def _serve_image(start_response, name: str):
    app = _get_app()
    assets = Path(app.bot_data["services"].settings.assets_dir).resolve()
    path = (assets / name).resolve()
    if assets not in path.parents or not path.is_file():
        return _respond(start_response, "404 Not Found")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not content_type.startswith("image/"):
        return _respond(start_response, "404 Not Found")
    return _respond(start_response, "200 OK", path.read_bytes(), content_type, "public, max-age=3600")


def _miniapp_api(environ, start_response, action: str):
    app = _get_app()
    svc = app.bot_data["services"]
    raw = _read_body(environ)
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return _json(start_response, "400 Bad Request", {"ok": False, "error": "bad_request"})
    user = verify_init_data(str(payload.get("initData", "")), svc.settings.bot_token)
    if user is None:
        log.warning("Mini App request rejected: initData missing, expired or not signed by this bot")
        return _json(start_response, "401 Unauthorized", {"ok": False, "error": "unauthorized"})

    svc.matchmaking.register_user(user["id"], user.get("first_name"), user.get("last_name"),
                                  user.get("username"), None, None)
    api = MiniAppAPI(svc)
    if action == "state":
        result = {"ok": True, "state": api.state(user["id"])}
    elif action == "answer":
        result = api.answer(user["id"], str(payload.get("guess", ""))[:500])
    else:
        result = api.hint(user["id"])
    # Deliver the chat side effects (next room, opponent alerts, free hints).
    try:
        _loop.run_until_complete(svc.notifier.flush())
    except Exception:
        log.exception("Failed to deliver messages after a Mini App action")
    return _json(start_response, "200 OK", result)


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "") or "/"

    if method == "GET" and path in ("/", "/health"):
        return _respond(start_response, "200 OK", b"Escape Room bot is running.\n")
    if method == "GET" and path in ("/app", "/app/"):
        return _respond(start_response, "200 OK", MINIAPP_PAGE.read_bytes(), "text/html; charset=utf-8")
    if method == "GET" and path.startswith("/app/img/"):
        return _serve_image(start_response, path[len("/app/img/"):])
    if path in ("/api/state", "/api/answer", "/api/hint"):
        if method != "POST":
            return _respond(start_response, "405 Method Not Allowed")
        return _miniapp_api(environ, start_response, path.rsplit("/", 1)[1])
    if path != WEBHOOK_PATH:
        return _respond(start_response, "404 Not Found")
    if method != "POST":
        return _respond(start_response, "405 Method Not Allowed")

    app = _get_app()
    expected = app.bot_data["services"].settings.webhook_secret
    received = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    if not expected or not hmac.compare_digest(received.encode(), expected.encode()):
        log.warning("Rejected a webhook request with a missing or wrong secret")
        return _respond(start_response, "403 Forbidden")

    raw = _read_body(environ)
    if raw is None:
        return _respond(start_response, "400 Bad Request")
    try:
        data = json.loads(raw)
        update = Update.de_json(data, app.bot)
    except Exception:
        log.warning("Ignoring an unreadable webhook body")
        return _respond(start_response, "400 Bad Request")

    try:
        _loop.run_until_complete(_handle(app, update))
    except Exception:
        # Still answer 200: a non-2xx makes Telegram re-send the same update
        # repeatedly. The error is logged, and game state is transactional.
        log.exception("Failed to process update %s", getattr(update, "update_id", "?"))
    return _respond(start_response, "200 OK")
