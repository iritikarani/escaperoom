import json

from tests.conftest import GROUP_A, GROUP_B, PUZZLES_PATH

HINTS = {}
for _p in json.loads(PUZZLES_PATH.read_text())["puzzles"]:
    HINTS.setdefault(_p["room"], _p["hints"])  # first puzzle per room (fixed draw)


def last_kind(world, chat, kind):
    rows = [m for m in world.outbox(chat) if m["kind"] == kind]
    return rows[-1]["text"] if rows else None


def test_paid_hint_deducts_and_delivers_first_hint(world):
    match_id = world.setup_1v1()
    out = world.hints.request_hint(1, 1, "private")
    assert out.data["hint_index"] == 0
    assert world.team(match_id, "A")["paid_hints_used"] == 1
    text = last_kind(world, 1, "paid_hint")
    assert HINTS[1][0] in text and "Paid hints left: <b>2 / 3</b>" in text


def test_hints_are_progressive_within_a_room(world):
    world.setup_1v1()
    for i in range(3):
        world.hints.request_hint(1, 1, "private")
        assert HINTS[1][i] in last_kind(world, 1, "paid_hint")


def test_opponent_gets_free_hint_for_their_current_room(world):
    match_id = world.setup_2v2()
    world.solve(1, GROUP_A, "supergroup", [1, 2, 3])   # Team A -> Room 4
    world.solve(3, GROUP_B, "supergroup", [1])         # Team B -> Room 2
    world.hints.request_hint(1, GROUP_A, "supergroup")
    a, b = world.team(match_id, "A"), world.team(match_id, "B")
    assert a["paid_hints_used"] == 1 and a["free_hints_received"] == 0
    assert b["paid_hints_used"] == 0 and b["free_hints_received"] == 1
    free = last_kind(world, GROUP_B, "free_hint")
    assert "Room 2" in free and HINTS[2][0] in free
    assert HINTS[4][0] not in free
    assert "opponent received a free hint" in last_kind(world, GROUP_A, "paid_hint")
    events = world.db.query_all("SELECT kind, room, team_id FROM hint_events ORDER BY event_id")
    assert [(e["kind"], e["room"]) for e in events] == [("paid", 4), ("free", 2)]


def test_free_hints_do_not_reduce_paid_allowance(world):
    match_id = world.setup_1v1()
    for _ in range(3):
        world.hints.request_hint(1, 1, "private")   # Bob: 3 free hints for Room 1
    bob = world.team(match_id, "B")
    assert bob["free_hints_received"] == 3 and bob["paid_hints_used"] == 0
    world.solve(2, 2, "private", [1])               # fresh room, full paid allowance
    out = world.hints.request_hint(2, 2, "private")
    assert out.text is None
    assert world.team(match_id, "B")["paid_hints_used"] == 1


def test_free_and_paid_hints_share_progression(world):
    world.setup_1v1()
    world.hints.request_hint(1, 1, "private")   # Bob gets free hint 1 for Room 1
    world.hints.request_hint(2, 2, "private")   # Bob's paid hint must be hint 2
    assert HINTS[1][1] in last_kind(world, 2, "paid_hint")


def test_exhausted_paid_hints(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", [1])
    world.hints.request_hint(1, 1, "private")
    world.solve(1, 1, "private", [2])
    world.hints.request_hint(1, 1, "private")
    world.hints.request_hint(1, 1, "private")
    out = world.hints.request_hint(1, 1, "private")
    assert "No paid hints left" in out.text
    assert world.team(match_id, "A")["paid_hints_used"] == 3
    assert world.team(match_id, "B")["free_hints_received"] == 3


def test_three_paid_hints_per_game_not_per_room(world):
    match_id = world.setup_1v1()
    for room in (1, 2, 3):
        assert world.hints.request_hint(1, 1, "private").text is None
        world.solve(1, 1, "private", [room])
    assert "No paid hints left" in world.hints.request_hint(1, 1, "private").text
    assert world.team(match_id, "A")["paid_hints_used"] == 3


def test_no_paid_hint_spent_when_room_hints_exhausted(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", [1, 2])  # Alice in Room 3, Bob in Room 1
    for _ in range(3):
        world.hints.request_hint(1, 1, "private")   # Bob receives all 3 hints for Room 1
    out = world.hints.request_hint(2, 2, "private")
    assert "already have all 3 hints" in out.text
    assert world.team(match_id, "B")["paid_hints_used"] == 0


def test_no_free_hint_when_opponent_already_has_all(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", [1])        # Alice -> Room 2, Bob in Room 1
    for _ in range(3):
        world.hints.request_hint(1, 1, "private")    # Bob gets all 3 free hints for Room 1... but
    # Alice has no paid hints left now; let Bob advance and pay for one in Room 2.
    world.solve(2, 2, "private", [1])
    # Bob in Room 2. Alice in Room 2 has hints_revealed 3 (her own paid ones).
    out = world.hints.request_hint(2, 2, "private")
    assert out.data["opponent_got_free"] is False
    assert world.team(match_id, "A")["free_hints_received"] == 0
    assert any("no free hint" in t for t in world.texts(1))


def test_no_free_hint_for_escaped_opponent_and_no_hints_after_end(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", range(1, 8))
    out = world.hints.request_hint(2, 2, "private")
    assert "over" in out.text
    assert world.team(match_id, "B")["paid_hints_used"] == 0
    assert world.hints.request_hint(1, 1, "private").text


def test_hints_not_available_in_lobby(world):
    world.user(1, "A")
    world.mm.create_match(1, "1v1")
    assert "hasn't started" in world.hints.request_hint(1, 1, "private").text


def test_2v2_hints_visible_to_both_teammates_via_group(world):
    world.setup_2v2()
    world.hints.request_hint(2, GROUP_A, "supergroup")
    text = last_kind(world, GROUP_A, "paid_hint")
    assert "requested by Ben2" in text
    assert last_kind(world, GROUP_B, "free_hint")
    assert "team group" in world.hints.request_hint(1, 1, "private").text


def test_hint_progress_tracked_per_side(world):
    world.setup_1v1()
    world.hints.request_hint(1, 1, "private")  # Alice: hint 1 Room 1 (paid); Bob: hint 1 Room 1 (free)
    world.hints.request_hint(1, 1, "private")  # Alice: hint 2; Bob: hint 2
    rows = world.db.query_all("SELECT team_id, room, hints_revealed FROM hint_progress ORDER BY team_id")
    assert [r["hints_revealed"] for r in rows] == [2, 2]
    assert HINTS[1][1] in last_kind(world, 2, "free_hint")
