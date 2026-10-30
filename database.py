"""SQLite persistence layer.

The database is the single source of truth for every match. Nothing about
game state is kept only in memory, so a restart never loses progress.

Concurrency model
-----------------
* Every state change runs inside :meth:`Database.transaction`, which opens a
  ``BEGIN IMMEDIATE`` transaction. SQLite then allows exactly one writer at a
  time, across threads *and* processes, so two simultaneous correct answers
  (or hint requests) are serialised and the second one sees the first one's
  committed result.
* Critical updates are additionally written as compare-and-set statements
  (``... WHERE current_room = ?`` / ``... WHERE status = 'active'``) so even a
  logic bug cannot advance a room twice or crown two winners.

Migrations
----------
``MIGRATIONS`` is an append-only list. Each entry is applied once, in order,
inside its own transaction, and recorded in ``schema_migrations``. To change
the schema, append a new ``(version, name, statements)`` tuple - never edit
an entry that has already shipped.

Run ``python database.py`` to create/upgrade the database file.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)

SCHEMA_V1: list[str] = [
    """
    CREATE TABLE users (
        user_id         INTEGER PRIMARY KEY,
        username        TEXT,
        first_name      TEXT NOT NULL DEFAULT '',
        last_name       TEXT,
        private_chat_id INTEGER,
        started_at      REAL,
        created_at      REAL NOT NULL,
        updated_at      REAL NOT NULL
    )
    """,
    """
    CREATE TABLE matches (
        match_id        TEXT PRIMARY KEY,
        mode            TEXT NOT NULL CHECK (mode IN ('1v1', '2v2')),
        status          TEXT NOT NULL
                        CHECK (status IN ('lobby', 'active', 'completed', 'cancelled', 'expired')),
        creator_id      INTEGER NOT NULL REFERENCES users(user_id),
        puzzle_snapshot TEXT NOT NULL,
        created_at      REAL NOT NULL,
        expires_at      REAL NOT NULL,
        started_at      REAL,
        ended_at        REAL,
        winner_team_id  INTEGER REFERENCES teams(team_id),
        end_reason      TEXT CHECK (end_reason IN ('escaped', 'forfeit', 'cancelled', 'expired'))
    )
    """,
    "CREATE INDEX idx_matches_status ON matches(status)",
    """
    CREATE TABLE teams (
        team_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id            TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
        label               TEXT NOT NULL CHECK (label IN ('A', 'B')),
        join_code           TEXT UNIQUE,
        capacity            INTEGER NOT NULL CHECK (capacity IN (1, 2)),
        chat_id             INTEGER,
        current_room        INTEGER NOT NULL DEFAULT 1,
        paid_hints_used     INTEGER NOT NULL DEFAULT 0 CHECK (paid_hints_used BETWEEN 0 AND 3),
        free_hints_received INTEGER NOT NULL DEFAULT 0,
        escaped_at          REAL,
        UNIQUE (match_id, label)
    )
    """,
    "CREATE INDEX idx_teams_match ON teams(match_id)",
    "CREATE INDEX idx_teams_chat ON teams(chat_id)",
    # Team membership doubles as the match-participant registry: the
    # UNIQUE(match_id, user_id) constraint makes it impossible for one user to
    # be registered twice in a match or on both sides of it.
    """
    CREATE TABLE team_members (
        team_id   INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
        match_id  TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
        user_id   INTEGER NOT NULL REFERENCES users(user_id),
        joined_at REAL NOT NULL,
        PRIMARY KEY (team_id, user_id),
        UNIQUE (match_id, user_id)
    )
    """,
    "CREATE INDEX idx_team_members_user ON team_members(user_id)",
    """
    CREATE TABLE linked_groups (
        link_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER NOT NULL,
        team_id     INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
        match_id    TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
        title       TEXT,
        linked_by   INTEGER NOT NULL REFERENCES users(user_id),
        linked_at   REAL NOT NULL,
        released_at REAL
    )
    """,
    # While a link is live, a group belongs to exactly one team and a team to
    # exactly one group. Released links (finished matches) free the group.
    "CREATE UNIQUE INDEX uq_linked_groups_chat_live ON linked_groups(chat_id) WHERE released_at IS NULL",
    "CREATE UNIQUE INDEX uq_linked_groups_team_live ON linked_groups(team_id) WHERE released_at IS NULL",
    """
    CREATE TABLE hint_progress (
        team_id        INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
        room           INTEGER NOT NULL,
        hints_revealed INTEGER NOT NULL DEFAULT 0 CHECK (hints_revealed BETWEEN 0 AND 3),
        PRIMARY KEY (team_id, room)
    )
    """,
    """
    CREATE TABLE hint_events (
        event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id             TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
        team_id              INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
        room                 INTEGER NOT NULL,
        hint_index           INTEGER NOT NULL,
        kind                 TEXT NOT NULL CHECK (kind IN ('paid', 'free')),
        requested_by         INTEGER REFERENCES users(user_id),
        triggered_by_team_id INTEGER REFERENCES teams(team_id),
        created_at           REAL NOT NULL
    )
    """,
    "CREATE INDEX idx_hint_events_team ON hint_events(team_id)",
    """
    CREATE TABLE room_completions (
        completion_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id        TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
        team_id         INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
        room            INTEGER NOT NULL,
        solved_by       INTEGER NOT NULL REFERENCES users(user_id),
        completed_at    REAL NOT NULL,
        elapsed_seconds REAL NOT NULL,
        UNIQUE (team_id, room)
    )
    """,
    """
    CREATE TABLE guess_attempts (
        attempt_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id         TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
        team_id          INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
        room             INTEGER NOT NULL,
        user_id          INTEGER NOT NULL REFERENCES users(user_id),
        guess_normalized TEXT NOT NULL,
        is_correct       INTEGER NOT NULL,
        created_at       REAL NOT NULL
    )
    """,
    "CREATE INDEX idx_guess_attempts_team_time ON guess_attempts(team_id, created_at)",
    """
    CREATE TABLE match_results (
        match_id                   TEXT PRIMARY KEY REFERENCES matches(match_id) ON DELETE CASCADE,
        winner_team_id             INTEGER NOT NULL REFERENCES teams(team_id),
        loser_team_id              INTEGER NOT NULL REFERENCES teams(team_id),
        result_type                TEXT NOT NULL CHECK (result_type IN ('escaped', 'forfeit')),
        duration_seconds           REAL NOT NULL,
        winner_paid_hints_used     INTEGER NOT NULL,
        winner_free_hints_received INTEGER NOT NULL,
        winner_room_reached        INTEGER NOT NULL,
        loser_room_reached         INTEGER NOT NULL,
        forfeited_by               INTEGER REFERENCES users(user_id),
        recorded_at                REAL NOT NULL
    )
    """,
    # Transactional outbox: messages are written in the same transaction as
    # the state change that caused them, then delivered by notifications.py.
    # dedupe_key makes every logical message (e.g. "Room 3 for team 12")
    # exist at most once, which is what keeps restarts from re-sending puzzles.
    """
    CREATE TABLE outbox (
        message_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id        TEXT REFERENCES matches(match_id) ON DELETE CASCADE,
        chat_id         INTEGER NOT NULL,
        kind            TEXT NOT NULL,
        payload         TEXT NOT NULL,
        dedupe_key      TEXT NOT NULL UNIQUE,
        status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
        attempts        INTEGER NOT NULL DEFAULT 0,
        last_error      TEXT,
        created_at      REAL NOT NULL,
        next_attempt_at REAL NOT NULL,
        sent_at         REAL
    )
    """,
    "CREATE INDEX idx_outbox_pending ON outbox(status, next_attempt_at)",
    """
    CREATE TABLE processed_updates (
        update_id    INTEGER PRIMARY KEY,
        processed_at REAL NOT NULL
    )
    """,
    "CREATE INDEX idx_processed_updates_time ON processed_updates(processed_at)",
    """
    CREATE TABLE join_failures (
        failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX idx_join_failures_user_time ON join_failures(user_id, created_at)",
]

MIGRATIONS: list[tuple[int, str, Sequence[str]]] = [
    (1, "initial schema", SCHEMA_V1),
]


class Database:
    """Thin wrapper around one SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we manage transactions explicitly.
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        self._lock = threading.RLock()

    # -- transactions -------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically. Nested calls join the outer transaction."""
        with self._lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # -- convenience reads (autocommit) -------------------------------------
    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.transaction() as conn:
            return conn.execute(sql, params)

    # -- schema -------------------------------------------------------------
    def schema_version(self) -> int:
        row = self.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        if row is None:
            return 0
        row = self.query_one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"] or 0)

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        with self.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    INTEGER PRIMARY KEY,
                    name       TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
        current = self.schema_version()
        for version, name, statements in MIGRATIONS:
            if version <= current:
                continue
            log.info("Applying migration %s: %s", version, name)
            with self.transaction() as conn:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                    (version, name),
                )
        return self.schema_version()

    # -- update de-duplication ---------------------------------------------
    def mark_update_processed(self, update_id: int, now: float) -> bool:
        """Record a Telegram update id. Returns False if it was seen before."""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_updates (update_id, processed_at) VALUES (?, ?)",
                (update_id, now),
            )
            return cur.rowcount == 1

    def prune(self, now: float, keep_seconds: float = 7 * 24 * 3600) -> None:
        cutoff = now - keep_seconds
        with self.transaction() as conn:
            conn.execute("DELETE FROM processed_updates WHERE processed_at < ?", (cutoff,))
            conn.execute("DELETE FROM join_failures WHERE created_at < ?", (cutoff,))

    def close(self) -> None:
        with self._lock:
            self.conn.close()


if __name__ == "__main__":
    from config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings(require_token=False)
    db = Database(settings.database_path)
    version = db.migrate()
    db.close()
    print(f"Database ready at {settings.database_path} (schema version {version}).")
