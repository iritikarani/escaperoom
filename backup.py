"""Make a consistent copy of the SQLite database while the bot is running.

Usage: python backup.py [destination.db]

Uses SQLite's online backup API, so it is safe to run against a live
database (unlike copying the file, which can capture a half-written WAL).
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from config import BASE_DIR, load_settings


def backup(source: Path, destination: Path) -> Path:
    if not Path(source).is_file():
        raise SystemExit(f"No database at {source} - nothing to back up.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return destination


if __name__ == "__main__":
    settings = load_settings(require_token=False)
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        target = BASE_DIR / "backups" / time.strftime("escape_room-%Y%m%d-%H%M%S.db")
    print(f"Backed up {settings.database_path} -> {backup(settings.database_path, target)}")
