"""Application configuration.

Everything that varies between environments is read from environment
variables (optionally loaded from a local ``.env`` file via python-dotenv).
Game-rule constants that must be identical for every match live here too so
that the engine, hint system and tests all agree on them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# --- Game rules (fixed by design; changing them changes the game) ---------
TOTAL_ROOMS = 7
PAID_HINTS_PER_SIDE = 3
HINTS_PER_ROOM = 3
TEAM_SIZE = {"1v1": 1, "2v2": 2}
MODES = tuple(TEAM_SIZE)

# A side whose current_room equals this value has escaped Room 7.
ESCAPED_ROOM = TOTAL_ROOMS + 1

MAX_ANSWER_LENGTH = 200


class ConfigError(RuntimeError):
    """Raised when the environment is missing required configuration."""


def _path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    database_path: Path = BASE_DIR / "data" / "escape_room.db"
    puzzles_path: Path = BASE_DIR / "puzzles" / "puzzles.json"
    assets_dir: Path = BASE_DIR / "assets" / "puzzle_images"
    lobby_ttl_seconds: int = 24 * 3600
    # Maximum guesses one side may submit per rolling minute. Stops scripted
    # brute-forcing of numeric locks, which would be unfair to the opponent.
    answer_rate_limit_per_minute: int = 15
    # Maximum invalid join codes one user may try per 10 minutes.
    join_failure_limit: int = 10
    outbox_max_attempts: int = 8
    outbox_poll_seconds: float = 10.0
    webhook_url: str = ""
    webhook_listen: str = "0.0.0.0"
    webhook_port: int = 8443
    webhook_secret: str = ""
    # Outbound HTTP proxy for Telegram API calls. PythonAnywhere's free plan
    # only allows outbound traffic through http://proxy.server:3128.
    telegram_proxy_url: str = ""
    # Public HTTPS address of the Mini App page (webhook hosting only), e.g.
    # https://you.pythonanywhere.com/app. Empty = Mini App disabled.
    webapp_url: str = ""
    # Optional direct link from BotFather's /newapp (https://t.me/<bot>/<name>).
    # Telegram only allows Mini App buttons in groups via such a link.
    miniapp_link: str = ""
    log_level: str = "INFO"


def load_settings(require_token: bool = True) -> Settings:
    """Build :class:`Settings` from the environment (and ``.env``)."""
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if require_token and not token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and "
            "paste the token you received from @BotFather."
        )

    return Settings(
        bot_token=token,
        database_path=_path(os.getenv("DATABASE_PATH"), Settings.database_path),
        puzzles_path=_path(os.getenv("PUZZLES_PATH"), Settings.puzzles_path),
        assets_dir=_path(os.getenv("ASSETS_DIR"), Settings.assets_dir),
        lobby_ttl_seconds=_int("LOBBY_TTL_HOURS", 24) * 3600,
        answer_rate_limit_per_minute=_int("ANSWER_RATE_LIMIT_PER_MINUTE", 15),
        join_failure_limit=_int("JOIN_FAILURE_LIMIT", 10),
        outbox_max_attempts=_int("OUTBOX_MAX_ATTEMPTS", 8),
        webhook_url=os.getenv("WEBHOOK_URL", "").strip(),
        webhook_listen=os.getenv("WEBHOOK_LISTEN", "0.0.0.0").strip(),
        webhook_port=_int("PORT", _int("WEBHOOK_PORT", 8443)),
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip(),
        webapp_url=os.getenv("WEBAPP_URL", "").strip(),
        miniapp_link=os.getenv("MINIAPP_LINK", "").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
