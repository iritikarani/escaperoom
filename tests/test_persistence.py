import sqlite3
import threading

import pytest

from database import MIGRATIONS, Database
from game_engine import GameEngine
from hint_manager import HintManager
from tests.conftest import ANSWERS, GROUP_A, GROUP_B


def test_migrations_are_idempotent(tmp_path):
    db = Database(tmp_path / "m.db")
    assert db.migrate() == MIGRATIONS[-1][0]
    assert db.migrate() == MIGRATIONS[-1][0]
    tables = {r["name"] for r in db.query_all("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ["users", "matches", "teams", "team_members", "linked_groups", "hint_progress",
              "hint_events", "room_completions", "match_results", "outbox", "processed_updates"]:
        assert t in tables
    db.close()


def test_restart_preserves_active_match(world):
    match_id = world.setup_2v2()
    world.solve(1, GROUP_A, "supergroup", [1, 2])
    world.hints.request_hint(1, GROUP_A, "supergroup")
    started = world.match(match_id)["started_at"]
    room_rows_before = world.db.query_one("SELECT COUNT(*) AS n FROM outbox WHERE kind = 'room'")["n"]

    world.restart()
    summary = world.engine.recover()
    assert summary["active_matches"] == 1

    a, b = world.team(match_id, "A"), world.team(match_id, "B")
    assert a["current_room"] == 3 and a["paid_hints_used"] == 1 and a["chat_id"] == GROUP_A
    assert b["current_room"] == 1 and b["free_hints_received"] == 1 and b["chat_id"] == GROUP_B
    assert world.match(match_id)["started_at"] == started
    # recovery does not queue duplicate puzzles
    assert world.db.query_one("SELECT COUNT(*) AS n FROM outbox WHERE kind = 'room'")["n"] == room_rows_before
    # hint progression continues where it left off
    world.hints.request_hint(2, GROUP_A, "supergroup")
    assert world.db.query_one(
        "SELECT hints_revealed FROM hint_progress WHERE team_id = ? AND room = 3", (a["team_id"],)
    )["hints_revealed"] == 2
    # play continues
    world.solve(2, GROUP_A, "supergroup", [3])
    assert world.team(match_id, "A")["current_room"] == 4


def test_recovery_queues_missing_room_delivery(world):
    match_id = world.setup_1v1()
    team = world.team(match_id, "A")
    # Simulate an outbox row lost before this version (or manual cleanup).
    world.db.execute("DELETE FROM outbox WHERE dedupe_key = ?", (f"room:{team['team_id']}:1",))
    world.restart()
    world.engine.recover()
    assert world.db.query_one(
        "SELECT COUNT(*) AS n FROM outbox WHERE dedupe_key = ?", (f"room:{team['team_id']}:1",))["n"] == 1


def test_restart_keeps_lobby_and_timers(world):
    world.user(1, "A")
    world.user(2, "B")
    created = world.mm.create_match(1, "1v1")
    world.restart()
    out = world.mm.join(2, created.code_b, 2, "private")
    assert "joined" in out.text
    assert world.match(created.match_id)["status"] == "active"


def test_duplicate_update_ids_are_ignored(world):
    assert world.db.mark_update_processed(42, 1.0) is True
    assert world.db.mark_update_processed(42, 2.0) is False
    world.restart()
    assert world.db.mark_update_processed(42, 3.0) is False


def test_database_error_rolls_back_whole_action(world, monkeypatch):
    match_id = world.setup_1v1()
    import game_engine

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(game_engine, "enqueue_room", boom)
    with pytest.raises(sqlite3.OperationalError):
        world.answer(1, 1, "private", ANSWERS[1])
    # the room advance and completion were rolled back with the failed enqueue
    assert world.team(match_id, "A")["current_room"] == 1
    assert world.db.query_one("SELECT COUNT(*) AS n FROM room_completions")["n"] == 0
    monkeypatch.undo()
    assert world.answer(1, 1, "private", ANSWERS[1]).text is None


def _engines(world, n):
    """Independent connections to the same file, like separate workers."""
    out = []
    for _ in range(n):
        db = Database(world.path)
        out.append((db, GameEngine(db, world.settings, world.clock), HintManager(db, world.settings, world.clock)))
    return out


def test_simultaneous_final_answers_produce_one_winner(world):
    for trial in range(5):
        a, b = 100 + trial * 2, 101 + trial * 2
        match_id = world.setup_1v1(a, b)
        world.solve(a, a, "private", range(1, 7))
        world.solve(b, b, "private", range(1, 7))
        (db1, e1, _), (db2, e2, _) = _engines(world, 2)
        barrier = threading.Barrier(2)
        results = {}

        def go(name, engine, uid):
            barrier.wait()
            results[name] = engine.submit_answer(uid, uid, "private", ANSWERS[7])

        threads = [threading.Thread(target=go, args=("a", e1, a)), threading.Thread(target=go, args=("b", e2, b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [k for k, v in results.items() if v.text is None]
        assert len(winners) == 1, results
        assert world.db.query_one(
            "SELECT COUNT(*) AS n FROM match_results WHERE match_id = ?", (match_id,))["n"] == 1
        escaped = world.db.query_all(
            "SELECT * FROM teams WHERE match_id = ? AND current_room = 8", (match_id,))
        assert len(escaped) == 1
        db1.close()
        db2.close()


def test_simultaneous_teammate_answers_advance_once(world):
    match_id = world.setup_2v2()
    engines = _engines(world, 2)
    barrier = threading.Barrier(2)
    results = []

    def go(engine, uid):
        barrier.wait()
        results.append(engine.submit_answer(uid, GROUP_A, "supergroup", ANSWERS[1]))

    threads = [threading.Thread(target=go, args=(engines[i][1], uid)) for i, uid in enumerate((1, 2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert world.team(match_id, "A")["current_room"] == 2
    assert world.db.query_one("SELECT COUNT(*) AS n FROM room_completions")["n"] == 1
    assert sum(r.text is None for r in results) == 1
    for db, *_ in engines:
        db.close()


def test_simultaneous_hint_requests_never_exceed_allowance(world):
    match_id = world.setup_2v2()
    engines = _engines(world, 6)
    barrier = threading.Barrier(6)

    def go(hints, uid):
        barrier.wait()
        hints.request_hint(uid, GROUP_A, "supergroup")

    threads = [threading.Thread(target=go, args=(engines[i][2], 1 + i % 2)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    a = world.team(match_id, "A")
    assert a["paid_hints_used"] == 3
    assert world.db.query_one("SELECT COUNT(*) AS n FROM hint_events WHERE kind='paid'")["n"] == 3
    for db, *_ in engines:
        db.close()
