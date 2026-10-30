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
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging

from telegram import Update
from telegram.ext import Application

import bot
from config import load_settings

log = logging.getLogger("escape_room.webhook")

WEBHOOK_PATH = "/telegram"
MAX_BODY_BYTES = 1_000_000

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


def _respond(start_response, status: str, body: bytes = b"") -> list[bytes]:
    start_response(status, [("Content-Type", "text/plain; charset=utf-8"),
                            ("Content-Length", str(len(body)))])
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "") or "/"

    if method == "GET" and path in ("/", "/health"):
        return _respond(start_response, "200 OK", b"Escape Room bot is running.\n")
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

    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > MAX_BODY_BYTES:
        return _respond(start_response, "400 Bad Request")
    try:
        data = json.loads(environ["wsgi.input"].read(length))
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
