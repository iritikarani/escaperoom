"""One-command setup for PythonAnywhere (free plan, webhook mode).

Run it in a PythonAnywhere Bash console, from the project folder, after
creating the web app (Web tab -> Add a new web app -> Manual configuration):

    .venv/bin/python pythonanywhere_setup.py

It asks for your bot token (typed input is hidden), then:

1. writes ``.env`` (token, a random webhook secret, the PythonAnywhere proxy),
2. creates/upgrades the SQLite database and validates puzzles.json,
3. points your web app's WSGI file at ``webhook_app.py``,
4. tells Telegram to send updates to https://<you>.pythonanywhere.com/telegram,
5. turns on the Mini App (the visual room screen) at /app and adds a "Play"
   button next to the message box in the bot's private chat.

For Mini App buttons inside 2v2 groups, create a direct link with
@BotFather's /newapp and pass it with ``--miniapp-link https://t.me/<bot>/<name>``.

Afterwards, press "Reload" on the Web tab. Re-running it is safe; it keeps
an existing token and secret unless you pass ``--new-token``.

``--remove-webhook`` switches Telegram back to polling mode (for running
``python bot.py`` somewhere else).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
PA_PROXY = "http://proxy.server:3128"
TOKEN_PATTERN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = ["# Written by pythonanywhere_setup.py - keep this file private."]
    lines += [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def telegram(token: str, method: str, payload: dict | None, proxy: str) -> dict:
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read())
        except ValueError:
            return {"ok": False, "description": f"HTTP {exc.code}"}


def wsgi_file_contents(project_dir: Path) -> str:
    return (
        "# Written by pythonanywhere_setup.py: serves the Escape Room bot webhook.\n"
        "import sys\n\n"
        f"PROJECT_DIR = {str(project_dir)!r}\n"
        "if PROJECT_DIR not in sys.path:\n"
        "    sys.path.insert(0, PROJECT_DIR)\n\n"
        "from webhook_app import application  # noqa: E402,F401\n"
    )


def fail(message: str) -> None:
    print(f"\n❌ {message}")
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", help="web app domain (default: <username>.pythonanywhere.com)")
    parser.add_argument("--new-token", action="store_true", help="ask for the bot token again")
    parser.add_argument("--miniapp-link", help="direct Mini App link from @BotFather /newapp, "
                                               "e.g. https://t.me/MyBot/room (enables the button in groups)")
    parser.add_argument("--no-miniapp", action="store_true", help="don't enable the Mini App room screen")
    parser.add_argument("--remove-webhook", action="store_true",
                        help="delete the Telegram webhook (switch back to polling) and exit")
    args = parser.parse_args(argv)

    username = getpass.getuser()
    domain = args.domain or f"{username}.pythonanywhere.com"
    on_pythonanywhere = bool(os.environ.get("PYTHONANYWHERE_DOMAIN")) or Path("/var/www").is_dir()

    env = read_env(ENV_PATH)
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if args.new_token or not TOKEN_PATTERN.match(token):
        print("Paste your bot token from @BotFather (the text is hidden as you type),")
        token = getpass.getpass("then press Enter: ").strip()
        if not TOKEN_PATTERN.match(token):
            fail("That doesn't look like a bot token (expected something like 123456789:AA...).")
    proxy = env.get("TELEGRAM_PROXY_URL") or (PA_PROXY if on_pythonanywhere else "")

    if args.remove_webhook:
        result = telegram(token, "deleteWebhook", {"drop_pending_updates": False}, proxy)
        print("✅ Webhook removed." if result.get("ok") else f"❌ {result.get('description')}")
        return 0 if result.get("ok") else 1

    print("\n1/4  Checking the token with Telegram...")
    me = telegram(token, "getMe", None, proxy)
    if not me.get("ok"):
        fail(f"Telegram rejected the token: {me.get('description')}. "
             "Run again with --new-token and paste it carefully.")
    bot_username = me["result"]["username"]
    print(f"     OK - this is @{bot_username}")

    env["TELEGRAM_BOT_TOKEN"] = token
    env["WEBHOOK_SECRET"] = env.get("WEBHOOK_SECRET") or secrets.token_urlsafe(32)
    if proxy:
        env["TELEGRAM_PROXY_URL"] = proxy
    if args.no_miniapp:
        env.pop("WEBAPP_URL", None)
        env.pop("MINIAPP_LINK", None)
    else:
        env["WEBAPP_URL"] = f"https://{domain}/app"
        if args.miniapp_link:
            if not re.match(r"^https://t\.me/\w+/\w+$", args.miniapp_link):
                fail("--miniapp-link should look like https://t.me/YourBot/appname")
            env["MINIAPP_LINK"] = args.miniapp_link
    write_env(ENV_PATH, env)
    os.environ.update(env)  # so the checks below use exactly what was saved
    print("2/4  Saved settings to .env (private).")

    sys.path.insert(0, str(BASE_DIR))
    from config import load_settings
    from database import Database
    from puzzle_manager import load_puzzles

    settings = load_settings()
    load_puzzles(settings.puzzles_path, settings.assets_dir)
    db = Database(settings.database_path)
    version = db.migrate()
    db.close()
    print(f"3/4  Database ready (schema v{version}), puzzles OK.")

    wsgi_path = Path("/var/www") / f"{domain.replace('.', '_')}_wsgi.py"
    if wsgi_path.parent.is_dir():
        if not wsgi_path.exists():
            fail(f"No web app found for {domain}. On the Web tab, add a new web app "
                 "(Manual configuration, Python 3.11 or newer), then run this again.")
        old = wsgi_path.read_text()
        if "webhook_app" not in old:
            wsgi_path.with_suffix(".py.bak").write_text(old)
        wsgi_path.write_text(wsgi_file_contents(BASE_DIR))
        print(f"     Web app now serves the bot ({wsgi_path}).")
    else:
        print("     (No /var/www here - point your WSGI server at webhook_app:application.)")

    url = f"https://{domain}/telegram"
    result = telegram(token, "setWebhook", {
        "url": url,
        "secret_token": env["WEBHOOK_SECRET"],
        "allowed_updates": ["message", "edited_message", "callback_query", "my_chat_member"],
        "drop_pending_updates": False,
    }, proxy)
    if not result.get("ok"):
        fail(f"Telegram refused the webhook: {result.get('description')}")
    print(f"4/4  Telegram will now send updates to {url}")

    if env.get("WEBAPP_URL"):
        menu = {"type": "web_app", "text": "Play", "web_app": {"url": env["WEBAPP_URL"]}}
        result = telegram(token, "setChatMenuButton", {"menu_button": menu}, proxy)
        if result.get("ok"):
            print(f"     Room screen (Mini App) is on: {env['WEBAPP_URL']}")
        else:
            print(f"     ⚠️ Couldn't add the Play button: {result.get('description')}")
    else:
        telegram(token, "setChatMenuButton", {"menu_button": {"type": "commands"}}, proxy)

    print(
        "\n🎉 Almost done! Last step:\n"
        "   Web tab -> press the green 'Reload' button.\n"
        f"   Then send /start to @{bot_username} in Telegram.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
