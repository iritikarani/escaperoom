"""Puzzle bank, per-player puzzle history and per-match puzzle selection.

The bank
--------
``puzzles/bank/bank.jsonl.gz`` holds 10,000 original sets of 7 puzzles
(70,000 puzzles, built by ``puzzle_generator.py``). ``import_bank`` loads it
into the ``puzzle_bank`` table; the hand-written puzzles in
``puzzles/puzzles.json`` are added too (``sync_curated``), so editing that file
still works. Sets are *not* used as fixed bundles: every puzzle can be
combined with puzzles from any other set.

Choosing a match's puzzles
--------------------------
When a match starts (all players known), ``PuzzleSelector.choose``:

1. looks up every participant's history (both players in 1v1, all four in
   2v2) - players are identified by their Telegram user id, so this works
   across devices and sessions;
2. for each room N, ranks the room-N puzzles of *all* sets by how many of the
   participants have seen them, then how often, then at random. Puzzles no
   participant has seen always come first; seen ones are only used when a
   room has run out of unseen ones, lowest exposure first;
3. picks one per room, never repeating a puzzle or an answer within a match,
   and re-picks if the exact same set of 7 puzzle ids (in any order) was
   ever handed out before (``match_combinations``);
4. returns one ordered ``PuzzleSet`` that is saved with the match - every
   player in the match plays exactly these puzzles in exactly this order.

Room N always draws from position N, which keeps the game's difficulty curve
(Easy rooms first, the Expert room last) while still mixing sets freely.

History
-------
``record_encounter`` adds a row whenever a room's puzzle is delivered to a
side, for every member of that side. Puzzles a side never reached don't count
as seen.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import secrets
import sqlite3
from pathlib import Path
from typing import Callable, Iterable

from answer_validator import canonical
from config import TOTAL_ROOMS
from database import Database
from puzzle_manager import PuzzleLibrary, PuzzleSet

log = logging.getLogger(__name__)

BANK_PATH = Path(__file__).resolve().parent / "puzzles" / "bank" / "bank.jsonl.gz"
CANDIDATES_PER_ROOM = 40
MAX_PICK_ATTEMPTS = 2000

# Which room-screen object each generated puzzle type is drawn with.
SCENES = {
    "fact_lock": "lock", "arith_lock": "lock", "sequence": "sign", "cipher": "scroll",
    "acrostic": "chest", "ordering": "hooks", "keypad": "keypad", "scatter": "letters",
    "code:affine": "ledger", "code:reverse": "scroll", "code:binary": "lamps", "code:keypad": "phone",
}


def combination_key(puzzle_ids: Iterable[str]) -> str:
    """Order-independent fingerprint of a combination of puzzles."""
    return hashlib.sha256("|".join(sorted(puzzle_ids)).encode()).hexdigest()


# -- importing ------------------------------------------------------------------

def _bank_row(rec: dict) -> tuple:
    family = rec["family"] + (f":{rec['meta']['kind']}" if rec["family"] == "code" else "")
    data = {
        "title": rec["title"], "difficulty": rec["difficulty"], "question": rec["question"],
        "accepted_answers": rec["accepted_answers"], "hints": rec["hints"],
        "solution": rec["solution"], "explanation": rec.get("explanation"),
        "scene": SCENES.get(family, "lock"),
    }
    return (rec["id"], rec["set"], rec["position"], rec["category"], rec["difficulty"], "generated",
            canonical(rec["solution"]), json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def import_bank(db: Database, path: Path = BANK_PATH, batch: int = 5000) -> int:
    """Load the generated bank into ``puzzle_bank``. Safe to run repeatedly."""
    from puzzle_generator import read_bank

    if not Path(path).exists():
        log.warning("Puzzle bank file %s not found", path)
        return 0
    rows, added = [], 0
    with db.transaction() as conn:
        for rec in read_bank(path):
            rows.append(_bank_row(rec))
            if len(rows) >= batch:
                added += _insert(conn, rows)
                rows = []
        if rows:
            added += _insert(conn, rows)
    return added


def _insert(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    before = conn.total_changes
    conn.executemany(
        """
        INSERT OR IGNORE INTO puzzle_bank
            (puzzle_id, set_id, position, category, difficulty, source, answer_key, data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    return conn.total_changes - before


def sync_curated(db: Database, library: PuzzleLibrary) -> int:
    """Add/refresh the hand-written puzzles from puzzles.json as extra sets.

    The k-th puzzle listed for each room belongs to set ``C<k>``.
    """
    rows = []
    for room in range(1, TOTAL_ROOMS + 1):
        for k, p in enumerate(library.options(room), start=1):
            data = {k2: v for k2, v in p.to_dict().items() if k2 not in ("id", "room")}
            rows.append((p.id, f"C{k}", room, "curated", p.difficulty, "curated",
                         canonical(p.solution), json.dumps(data, ensure_ascii=False)))
    with db.transaction() as conn:
        conn.executemany(
            """
            INSERT INTO puzzle_bank (puzzle_id, set_id, position, category, difficulty, source, answer_key, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (puzzle_id) DO UPDATE SET
                set_id = excluded.set_id, position = excluded.position, difficulty = excluded.difficulty,
                answer_key = excluded.answer_key, data = excluded.data
            WHERE puzzle_bank.source = 'curated'
            """,
            rows,
        )
        keep = [r[0] for r in rows]
        conn.execute(
            f"DELETE FROM puzzle_bank WHERE source = 'curated' AND puzzle_id NOT IN ({','.join('?' * len(keep))})",
            keep,
        )
    return len(rows)


def bank_counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS puzzles, COUNT(DISTINCT set_id) AS sets,
               SUM(source = 'generated') AS generated, SUM(source = 'curated') AS curated,
               COUNT(DISTINCT position) AS positions
        FROM puzzle_bank
        """
    ).fetchone()
    return {k: row[k] or 0 for k in row.keys()}


# -- history ----------------------------------------------------------------------

def record_encounter(conn: sqlite3.Connection, match_id: str, team_id: int, puzzle_id: str,
                     room: int, now: float) -> None:
    """Remember that every member of ``team_id`` was shown ``puzzle_id``."""
    if not puzzle_id:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO player_puzzle_history (user_id, puzzle_id, match_id, room, seen_at)
        SELECT user_id, ?, ?, ?, ? FROM team_members WHERE team_id = ?
        """,
        (puzzle_id, match_id, room, now, team_id),
    )


def backfill_history(db: Database) -> int:
    """Rebuild history for matches played before history was recorded."""
    added = 0
    with db.transaction() as conn:
        before = conn.total_changes
        for match in conn.execute(
            "SELECT match_id, puzzle_snapshot, started_at FROM matches WHERE started_at IS NOT NULL"
        ).fetchall():
            try:
                puzzles = list(PuzzleSet.from_json(match["puzzle_snapshot"]))
            except Exception:
                continue
            for team in conn.execute("SELECT team_id, current_room FROM teams WHERE match_id = ?",
                                     (match["match_id"],)).fetchall():
                for p in puzzles:
                    if p.room <= min(team["current_room"], TOTAL_ROOMS):
                        record_encounter(conn, match["match_id"], team["team_id"], p.id, p.room,
                                         match["started_at"])
        added = conn.total_changes - before
    return added


def exposure(conn: sqlite3.Connection, user_ids: list[int]) -> dict[str, tuple[int, int]]:
    """puzzle_id -> (number of these players who have seen it, total sightings)."""
    if not user_ids:
        return {}
    rows = conn.execute(
        f"""
        SELECT puzzle_id, COUNT(DISTINCT user_id) AS seen_by, COUNT(*) AS times
        FROM player_puzzle_history WHERE user_id IN ({','.join('?' * len(user_ids))})
        GROUP BY puzzle_id
        """,
        user_ids,
    ).fetchall()
    return {r["puzzle_id"]: (r["seen_by"], r["times"]) for r in rows}


# -- selection -------------------------------------------------------------------------

class Candidate:
    __slots__ = ("puzzle_id", "data", "answer_key", "seen_by", "times")

    def __init__(self, puzzle_id, data, answer_key, seen_by, times):
        self.puzzle_id, self.data, self.answer_key = puzzle_id, data, answer_key
        self.seen_by, self.times = seen_by, times

    @property
    def rank(self) -> tuple[int, int]:
        return (self.seen_by, self.times)


class PuzzleSelector:
    """Builds the 7-puzzle combination for a match. One instance per process."""

    def __init__(self, library_source: Callable[[], PuzzleLibrary], candidates: int = CANDIDATES_PER_ROOM):
        self.library_source = library_source
        self.candidates = candidates

    def bank_ready(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT COUNT(DISTINCT position) FROM puzzle_bank WHERE position BETWEEN 1 AND ?", (TOTAL_ROOMS,)
        ).fetchone()
        return row[0] == TOTAL_ROOMS

    def choose(self, conn: sqlite3.Connection, user_ids: Iterable[int], match_id: str, now: float,
               rng=None) -> PuzzleSet:
        rng = rng or secrets.SystemRandom()
        users = sorted(set(user_ids))
        if self.bank_ready(conn):
            pools = {room: self._bank_candidates(conn, users, room) for room in range(1, TOTAL_ROOMS + 1)}
        else:
            log.warning("Puzzle bank is empty - choosing from puzzles.json only. Run: python bot.py --init-db")
            pools = self._library_candidates(conn, users, rng)
        chosen = self._pick(conn, pools, rng)
        ids = [c.puzzle_id for c in chosen]
        conn.execute(
            "INSERT INTO match_combinations (combination_key, match_id, puzzle_ids, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (combination_key) DO NOTHING",
            (combination_key(ids), match_id, json.dumps(ids), now),
        )
        return PuzzleSet.from_data({"puzzles": [
            {**c.data, "id": c.puzzle_id, "room": room} for room, c in enumerate(chosen, start=1)
        ]})

    def _bank_candidates(self, conn, users: list[int], room: int) -> list[Candidate]:
        placeholders = ",".join("?" * len(users)) or "NULL"
        rows = conn.execute(
            f"""
            SELECT b.puzzle_id, b.data, b.answer_key,
                   COALESCE(h.seen_by, 0) AS seen_by, COALESCE(h.times, 0) AS times
            FROM puzzle_bank b
            LEFT JOIN (
                SELECT puzzle_id, COUNT(DISTINCT user_id) AS seen_by, COUNT(*) AS times
                FROM player_puzzle_history WHERE user_id IN ({placeholders})
                GROUP BY puzzle_id
            ) h ON h.puzzle_id = b.puzzle_id
            WHERE b.position = ?
            ORDER BY seen_by, times, RANDOM()
            LIMIT ?
            """,
            (*users, room, self.candidates),
        ).fetchall()
        return [Candidate(r["puzzle_id"], json.loads(r["data"]), r["answer_key"], r["seen_by"], r["times"])
                for r in rows]

    def _library_candidates(self, conn, users: list[int], rng) -> dict[int, list[Candidate]]:
        seen = exposure(conn, users)
        library = self.library_source()
        pools = {}
        for room in range(1, TOTAL_ROOMS + 1):
            cands = []
            for p in library.options(room):
                data = {k: v for k, v in p.to_dict().items() if k not in ("id", "room")}
                s, t = seen.get(p.id, (0, 0))
                cands.append(Candidate(p.id, data, canonical(p.solution), s, t))
            cands.sort(key=lambda c: (c.seen_by, c.times, rng.random()))
            pools[room] = cands
        return pools

    def _pick(self, conn, pools: dict[int, list[Candidate]], rng) -> list[Candidate]:
        """Best-first search for the fairest valid combination.

        Each state picks one candidate per room. States are explored cheapest
        first (fewest participants who have seen the puzzles, then fewest
        sightings), so a fresh combination always wins over one that reuses
        seen puzzles. A state is valid when no puzzle or answer repeats and
        its exact set of puzzle ids has never been handed out before.
        """
        rooms = sorted(pools)
        tiebreak = {(r, i): rng.random() for r in rooms for i in range(len(pools[r]))}

        def cost(state):
            picks = [pools[r][i] for r, i in zip(rooms, state)]
            return (sum(c.seen_by for c in picks), sum(c.times for c in picks),
                    sum(tiebreak[(r, i)] for r, i in zip(rooms, state)))

        start = tuple(0 for _ in rooms)
        heap, queued, fallback = [(cost(start), start)], {start}, None
        for _ in range(MAX_PICK_ATTEMPTS):
            if not heap:
                break
            _, state = heapq.heappop(heap)
            chosen = [pools[r][i] for r, i in zip(rooms, state)]
            distinct = (len({c.puzzle_id for c in chosen}) == len(chosen)
                        and len({c.answer_key for c in chosen}) == len(chosen))
            if distinct:
                fallback = fallback or chosen
                used = conn.execute("SELECT 1 FROM match_combinations WHERE combination_key = ?",
                                    (combination_key(c.puzzle_id for c in chosen),)).fetchone()
                if used is None:
                    return chosen
            for k, r in enumerate(rooms):
                if state[k] + 1 < len(pools[r]):
                    nxt = state[:k] + (state[k] + 1,) + state[k + 1:]
                    if nxt not in queued:
                        queued.add(nxt)
                        heapq.heappush(heap, (cost(nxt), nxt))
        # Every reachable combination has been used (only possible with a tiny
        # pool): still start the match rather than block it.
        log.warning("No unused puzzle combination left; reusing one")
        return fallback or [pools[r][0] for r in rooms]
