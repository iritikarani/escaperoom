"""Puzzle bank, puzzle mixing, player history and combination tracking.

Most tests use a small bank built by the real generator so every scenario
(exhausted pools, collisions) can be reached quickly; the committed
70,000-puzzle bank file is checked and imported in full as well.
"""

import collections
import itertools
import json
import time

import pytest

import puzzle_generator as gen
from answer_validator import canonical
from database import Database
from puzzle_bank import (PuzzleSelector, backfill_history, bank_counts, combination_key, import_bank,
                         sync_curated)
from puzzle_manager import PuzzleSet, load_puzzles
from tests.conftest import ASSETS_DIR, PUZZLES_PATH


def build_bank(tmp_path, sets):
    path = tmp_path / f"bank{sets}.jsonl.gz"
    gen.write_bank(gen.generate_bank(sets, seed="test-bank"), path)
    return path


@pytest.fixture
def bank(world, tmp_path):
    """The world's database with a 30-set generated bank loaded."""
    assert import_bank(world.db, build_bank(tmp_path, 30)) == 210
    return world


def set_of(puzzle_id):
    return puzzle_id.split("-R")[0]


def history(world, user_id):
    return {r["puzzle_id"] for r in world.db.query_all(
        "SELECT puzzle_id FROM player_puzzle_history WHERE user_id = ?", (user_id,))}


def seed_history(world, user_id, puzzle_ids):
    """Pretend ``user_id`` saw ``puzzle_ids`` in an earlier match."""
    if world.db.query_one("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) is None:
        world.user(user_id, f"P{user_id}")
    now = world.clock()
    with world.db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO matches (match_id, mode, status, creator_id, puzzle_snapshot, created_at, expires_at)"
            " VALUES ('SEEDED', '1v1', 'completed', ?, '{}', ?, ?)", (user_id, now, now))
        conn.executemany("INSERT OR IGNORE INTO player_puzzle_history VALUES (?, ?, 'SEEDED', 1, ?)",
                         [(user_id, p, now) for p in puzzle_ids])


def room_ids(world, room):
    return [r["puzzle_id"] for r in world.db.query_all(
        "SELECT puzzle_id FROM puzzle_bank WHERE position = ? ORDER BY puzzle_id", (room,))]


_uid = itertools.count(1000)


def fresh_1v1(world):
    a, b = next(_uid), next(_uid)
    return world.setup_1v1(a, b), (a, b)


# -- the committed bank -----------------------------------------------------------------

def test_committed_bank_has_10000_sets_of_7_verified_puzzles():
    records = list(gen.read_bank())
    assert len(records) == 70_000
    by_set = collections.defaultdict(list)
    for r in records:
        by_set[r["set"]].append(r["position"])
    assert len(by_set) == 10_000
    assert all(sorted(p) == list(range(1, 8)) for p in by_set.values())
    assert len({r["id"] for r in records}) == 70_000
    for position in range(1, 8):
        column = [r for r in records if r["position"] == position]
        assert len({gen.core_key(r) for r in column}) == 10_000, position  # unique by content
        assert {r["difficulty"] for r in column} == {gen.DIFFICULTY[position]}
    for r in records:  # every answer re-derived independently from the puzzle's parameters
        gen.verify_record(r)


def test_committed_bank_imports_once(tmp_path):
    db = Database(tmp_path / "full.db")
    db.migrate()
    assert import_bank(db) == 70_000
    assert import_bank(db) == 0  # idempotent
    sync_curated(db, load_puzzles(PUZZLES_PATH, ASSETS_DIR))
    with db.transaction() as conn:
        counts = bank_counts(conn)
    assert counts["generated"] == 70_000 and counts["curated"] == 21
    assert counts["sets"] == 10_003 and counts["positions"] == 7
    db.close()


def test_generator_is_deterministic():
    a = gen.generate_bank(5, seed="same")
    b = gen.generate_bank(5, seed="same")
    assert a == b
    assert a != gen.generate_bank(5, seed="different")


# -- identical puzzles for everyone in a match ------------------------------------------------

def test_1v1_players_receive_identical_puzzles(bank):
    match_id, (a, b) = fresh_1v1(bank)
    ids = bank.snapshot_ids(match_id)
    assert len(ids) == 7 and [i.rsplit("-R", 1)[1] for i in ids] == [str(r) for r in range(1, 8)]
    room1 = lambda chat: [t for t in bank.texts(chat) if "ROOM 1 / 7" in t]
    assert room1(a) == room1(b) and len(room1(a)) == 1
    # Both players advance through the same puzzle in each room.
    bank.solve(a, a, "private", [1, 2])
    bank.solve(b, b, "private", [1])
    assert [t for t in bank.texts(a) if "ROOM 2 / 7" in t] == [t for t in bank.texts(b) if "ROOM 2 / 7" in t]


def test_2v2_all_four_players_receive_identical_puzzles(bank):
    match_id = bank.setup_2v2(ids=(201, 202, 203, 204), groups=(-801, -802))
    ids = bank.snapshot_ids(match_id)
    room1 = lambda chat: [t for t in bank.texts(chat) if "ROOM 1 / 7" in t]
    assert room1(-801) == room1(-802) and len(room1(-801)) == 1
    for uid in (201, 202, 203, 204):  # every player has the same first puzzle on record
        assert history(bank, uid) == {ids[0]}
    assert bank.db.query_one("SELECT COUNT(*) AS n FROM match_combinations WHERE match_id = ?",
                             (match_id,))["n"] == 1  # one combination per match


# -- mixing ---------------------------------------------------------------------------------------

def test_matches_mix_puzzles_from_different_sets(bank):
    mixed, all_sets = 0, set()
    for _ in range(15):
        match_id, _ = fresh_1v1(bank)
        sets = {set_of(i) for i in bank.snapshot_ids(match_id)}
        all_sets |= sets
        mixed += len(sets) > 1
    assert mixed >= 14  # with 30 sets, an unmixed (single-set) match is ~1 in 24 million
    assert len(all_sets) > 15


def test_room_n_always_comes_from_position_n(bank):
    for _ in range(5):
        match_id, _ = fresh_1v1(bank)
        assert [int(i.rsplit("-R", 1)[1]) for i in bank.snapshot_ids(match_id)] == list(range(1, 8))


# -- returning players and history ---------------------------------------------------------------

def test_returning_player_is_recognised_and_gets_unseen_puzzles(bank):
    first = bank.setup_1v1(301, 302)
    bank.solve(301, 301, "private", range(1, 8))  # Alice sees all seven puzzles and wins
    seen = history(bank, 301)
    assert seen == set(bank.snapshot_ids(first))
    second = bank.setup_1v1(301, 303)  # same Telegram account, new opponent
    assert not set(bank.snapshot_ids(second)) & seen
    rows = bank.db.query_all("SELECT DISTINCT match_id FROM player_puzzle_history WHERE user_id = 301")
    assert {r["match_id"] for r in rows} == {first, second}


def test_history_only_counts_rooms_actually_shown(bank):
    match_id = bank.setup_1v1(311, 312)
    ids = bank.snapshot_ids(match_id)
    assert history(bank, 311) == history(bank, 312) == {ids[0]}
    bank.solve(311, 311, "private", [1])
    assert history(bank, 311) == {ids[0], ids[1]}
    assert history(bank, 312) == {ids[0]}  # the opponent hasn't reached room 2


def test_1v1_checks_both_players_histories(bank):
    seed_history(bank, 321, room_ids(bank, 1)[:15])
    seed_history(bank, 322, room_ids(bank, 1)[15:29])
    unseen = room_ids(bank, 1)[29]
    for user in (321, 322):
        bank.user(user, f"P{user}")
    match_id = bank.setup_1v1(321, 322)
    assert bank.snapshot_ids(match_id)[0] == unseen


def test_2v2_checks_all_four_histories(bank):
    ids = room_ids(bank, 3)
    for i, uid in enumerate((331, 332, 333, 334)):
        bank.user(uid, f"P{uid}")
        seed_history(bank, uid, ids[i * 7:(i + 1) * 7])  # together they've seen 28 of 30
    match_id = bank.setup_2v2(ids=(331, 332, 333, 334), groups=(-811, -812))
    assert bank.snapshot_ids(match_id)[2] in ids[28:]


def test_unseen_puzzles_are_preferred_in_every_room(bank):
    everything_but_one = [room_ids(bank, r)[:-1] for r in range(1, 8)]
    bank.user(341, "A")
    bank.user(342, "B")
    seed_history(bank, 341, [p for room in everything_but_one for p in room])
    match_id = bank.setup_1v1(341, 342)
    assert bank.snapshot_ids(match_id) == [room_ids(bank, r)[-1] for r in range(1, 8)]


# -- fallback ----------------------------------------------------------------------------------------

def test_fallback_uses_lowest_exposure_when_nothing_is_unseen(bank):
    ids = room_ids(bank, 1)
    bank.user(351, "A")
    bank.user(352, "B")
    seed_history(bank, 351, ids)            # A has seen every room-1 puzzle
    seed_history(bank, 352, ids[1:])        # B has seen all but the first
    match_id = bank.setup_1v1(351, 352)
    assert bank.snapshot_ids(match_id)[0] == ids[0]  # seen by 1 player beats seen by 2


def test_fallback_mixes_unseen_and_seen_and_still_starts(bank):
    for uid in (361, 362):
        bank.user(uid, f"P{uid}")
    for r in (1, 2, 3):  # rooms 1-3 completely exhausted for both players
        seed_history(bank, 361, room_ids(bank, r))
        seed_history(bank, 362, room_ids(bank, r))
    pre_match = history(bank, 361) | history(bank, 362)
    match_id = bank.setup_1v1(361, 362)
    assert bank.match(match_id)["status"] == "active"
    ids = bank.snapshot_ids(match_id)
    assert all(i in pre_match for i in ids[:3])          # had to reuse
    assert not any(i in pre_match for i in ids[3:])      # but rooms 4-7 are all fresh
    assert len(set(ids)) == 7


# -- no repeats -------------------------------------------------------------------------------------

def test_no_puzzle_or_answer_twice_in_a_match(bank):
    for _ in range(20):
        match_id, _ = fresh_1v1(bank)
        puzzles = list(PuzzleSet.from_json(bank.match(match_id)["puzzle_snapshot"]))
        assert len({p.id for p in puzzles}) == 7
        assert len({canonical(p.solution) for p in puzzles}) == 7


def test_combination_key_ignores_order():
    ids = [f"S{i}-R{i}" for i in range(1, 8)]
    assert combination_key(ids) == combination_key(reversed(ids))
    assert combination_key(ids) != combination_key(ids[:6] + ["S9-R7"])


def test_exact_combinations_are_never_reused(world, tmp_path):
    import_bank(world.db, build_bank(tmp_path, 2))  # only 2^7 = 128 possible combinations
    keys = []
    for _ in range(40):
        match_id, (a, b) = fresh_1v1(world)
        keys.append(combination_key(world.snapshot_ids(match_id)))
    assert len(set(keys)) == 40


def test_last_unused_combination_is_found(world, tmp_path):
    import_bank(world.db, build_bank(tmp_path, 2))
    options = [room_ids(world, r) for r in range(1, 8)]
    combos = list(itertools.product(*options))
    left_over = combos[77]
    world.user(1, "Owner")
    now = world.clock()
    with world.db.transaction() as conn:
        for i, combo in enumerate(combos):
            if combo != left_over:
                conn.execute("INSERT INTO matches (match_id, mode, status, creator_id, puzzle_snapshot, created_at,"
                             " expires_at) VALUES (?, '1v1', 'completed', 1, '{}', ?, ?)", (f"OLD{i}", now, now))
                conn.execute("INSERT INTO match_combinations VALUES (?, ?, ?, ?)",
                             (combination_key(combo), f"OLD{i}", json.dumps(combo), now))
    match_id, _ = fresh_1v1(world)
    assert tuple(world.snapshot_ids(match_id)) == left_over


def test_match_still_starts_when_every_combination_is_used(world, tmp_path):
    import_bank(world.db, build_bank(tmp_path, 1))  # exactly one possible combination
    first, _ = fresh_1v1(world)
    second, _ = fresh_1v1(world)
    assert world.match(second)["status"] == "active"
    assert world.snapshot_ids(first) == world.snapshot_ids(second)


# -- fallback pool, curated puzzles, backfill -----------------------------------------------------

def test_without_the_bank_the_curated_pool_uses_the_same_rules(world):
    selector = PuzzleSelector(lambda: load_puzzles(PUZZLES_PATH, ASSETS_DIR))
    with world.db.transaction() as conn:
        assert not selector.bank_ready(conn)
    world.setup_1v1(371, 372)
    world.solve(371, 371, "private", range(1, 8))
    seen = history(world, 371)
    assert len(seen) == 7
    second = world.setup_1v1(371, 373)
    assert not set(world.snapshot_ids(second)) & seen


def test_curated_puzzles_join_the_bank(world):
    sync_curated(world.db, load_puzzles(PUZZLES_PATH, ASSETS_DIR))
    with world.db.transaction() as conn:
        counts = bank_counts(conn)
        assert PuzzleSelector(lambda: None).bank_ready(conn)
    assert counts["curated"] == 21 and counts["sets"] == 3  # C1, C2, C3
    match_id = world.setup_1v1(381, 382)
    assert all(not i.startswith("S") for i in world.snapshot_ids(match_id))


def test_backfill_rebuilds_history_for_old_matches(bank):
    bank.setup_1v1(391, 392)
    bank.solve(391, 391, "private", [1, 2])
    before = bank.db.query_all("SELECT user_id, puzzle_id FROM player_puzzle_history ORDER BY 1, 2")
    bank.db.execute("DELETE FROM player_puzzle_history")
    assert backfill_history(bank.db) == len(before)
    after = bank.db.query_all("SELECT user_id, puzzle_id FROM player_puzzle_history ORDER BY 1, 2")
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_bank_puzzles_play_end_to_end(bank):
    match_id, (a, b) = fresh_1v1(bank)
    bank.hints.request_hint(a, a, "private")
    bank.solve(a, a, "private", range(1, 8))
    assert bank.match(match_id)["winner_team_id"] == bank.team(match_id, "A")["team_id"]
    puzzle = PuzzleSet.from_json(bank.match(match_id)["puzzle_snapshot"]).get(6)
    assert puzzle.scene in {"ledger", "scroll", "lamps", "phone"}  # room screen art for the puzzle type


def test_selection_is_fast_on_the_full_bank(tmp_path):
    db = Database(tmp_path / "speed.db")
    db.migrate()
    import_bank(db)
    selector = PuzzleSelector(lambda: None)
    now = time.time()
    with db.transaction() as conn:
        for uid in (1, 2, 3, 4):
            conn.execute("INSERT INTO users (user_id, first_name, created_at, updated_at) VALUES (?, 'P', ?, ?)",
                         (uid, now, now))
        conn.execute("INSERT INTO matches (match_id, mode, status, creator_id, puzzle_snapshot, created_at,"
                     " expires_at) VALUES ('M1', '2v2', 'lobby', 1, '{}', ?, ?)", (now, now))
        start = time.perf_counter()
        puzzles = selector.choose(conn, [1, 2, 3, 4], "M1", now)
        elapsed = time.perf_counter() - start
    assert len(puzzles) == 7 and elapsed < 2.0
    db.close()


