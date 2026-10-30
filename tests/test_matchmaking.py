import pytest

from matchmaking import CODE_ALPHABET, CODE_LENGTH, normalize_code
from state import GameError
from tests.conftest import GROUP_A, GROUP_B


def test_create_1v1_match(world):
    world.user(1, "Alice")
    created = world.mm.create_match(1, "1v1")
    assert created.code_a is None
    assert len(created.code_b) == CODE_LENGTH and set(created.code_b) <= set(CODE_ALPHABET)
    match = world.match(created.match_id)
    assert match["mode"] == "1v1" and match["status"] == "lobby"
    assert world.team(created.match_id, "A")["chat_id"] == 1


def test_join_1v1_starts_match_and_delivers_room1_to_both(world):
    match_id = world.setup_1v1()
    match = world.match(match_id)
    assert match["status"] == "active" and match["started_at"] == world.clock.now
    for chat in (1, 2):
        texts = world.texts(chat)
        assert any("THE MATCH HAS BEGUN" in t for t in texts)
        rooms = [t for t in texts if "ROOM 1 / 7" in t]
        assert len(rooms) == 1
    # identical content on both sides
    room1 = lambda chat: [t for t in world.texts(chat) if "ROOM 1 / 7" in t][0]
    assert room1(1) == room1(2)
    # paid hints initialised
    for label in "AB":
        team = world.team(match_id, label)
        assert team["current_room"] == 1 and team["paid_hints_used"] == 0


def test_creator_is_told_when_opponent_joins(world):
    world.setup_1v1()
    assert any("joined your 1v1 match" in t for t in world.texts(1))


def test_join_via_deep_link_payload_is_normalised():
    assert normalize_code(" abcd-efgh ") == "ABCDEFGH"
    assert normalize_code("ABC") is None
    assert normalize_code("ABCDEFG0") is None  # 0 is not in the alphabet
    assert normalize_code("'; DROP TABLE") is None


def test_create_2v2_link_two_groups_register_four_players(world):
    match_id = world.setup_2v2()
    a, b = world.team(match_id, "A"), world.team(match_id, "B")
    assert a["chat_id"] == GROUP_A and b["chat_id"] == GROUP_B
    members = world.db.query_all("SELECT team_id, user_id FROM team_members WHERE match_id = ?", (match_id,))
    by_team = {}
    for m in members:
        by_team.setdefault(m["team_id"], set()).add(m["user_id"])
    assert by_team == {a["team_id"]: {1, 2}, b["team_id"]: {3, 4}}
    assert any("ROOM 1 / 7" in t for t in world.texts(GROUP_A))
    assert any("ROOM 1 / 7" in t for t in world.texts(GROUP_B))


def test_2v2_does_not_start_until_everyone_joined(world):
    for uid in (1, 2, 3, 4):
        world.user(uid, f"P{uid}")
    created = world.mm.create_match(1, "2v2")
    world.mm.join(1, created.code_a, GROUP_A, "group")
    world.mm.join(2, created.code_a, GROUP_A, "group")
    world.mm.join(3, created.code_b, GROUP_B, "group")
    assert world.match(created.match_id)["status"] == "lobby"
    assert world.texts(GROUP_A) == []  # no puzzle before the start
    world.mm.join(4, created.code_b, GROUP_B, "group")
    assert world.match(created.match_id)["status"] == "active"


def test_invalid_join_codes_rejected(world):
    world.user(1, "Alice")
    for code in ["", "nope", "ZZZZZZZZ", "ABCDEFGH"]:
        out = world.mm.join(1, code, 1, "private")
        assert "❌" in out.text


def test_expired_code_rejected(world):
    world.user(1, "Alice")
    world.user(2, "Bob")
    created = world.mm.create_match(1, "1v1")
    world.clock.advance(world.settings.lobby_ttl_seconds + 1)
    out = world.mm.join(2, created.code_b, 2, "private")
    assert "Invalid or expired" in out.text
    assert world.match(created.match_id)["status"] == "expired"


def test_join_failures_are_rate_limited(world):
    world.user(1, "Alice")
    for _ in range(world.settings.join_failure_limit):
        world.mm.join(1, "ZZZZZZZZ", 1, "private")
    out = world.mm.join(1, "ZZZZZZZZ", 1, "private")
    assert "Too many invalid codes" in out.text


def test_duplicate_registration_rejected(world):
    match_id = world.setup_1v1()
    code = world.team(match_id, "B")["join_code"]
    out = world.mm.join(2, code, 2, "private")
    assert "already started" in out.text
    count = world.db.query_one("SELECT COUNT(*) AS n FROM team_members WHERE match_id = ?", (match_id,))["n"]
    assert count == 2


def test_same_user_cannot_join_twice_in_lobby(world):
    for uid in (1, 2):
        world.user(uid, f"P{uid}")
    created = world.mm.create_match(1, "2v2")
    world.mm.join(2, created.code_a, GROUP_A, "group")
    out = world.mm.join(2, created.code_a, GROUP_A, "group")
    assert "already on Team A" in out.text
    assert world.db.query_one("SELECT COUNT(*) AS n FROM team_members WHERE user_id = 2")["n"] == 1


def test_user_cannot_join_both_teams(world):
    for uid in (1, 2):
        world.user(uid, f"P{uid}")
    created = world.mm.create_match(1, "2v2")
    world.mm.join(2, created.code_a, GROUP_A, "group")
    out = world.mm.join(2, created.code_b, GROUP_B, "group")
    assert "other side" in out.text


def test_creator_cannot_join_own_1v1_as_opponent(world):
    world.user(1, "Alice")
    created = world.mm.create_match(1, "1v1")
    out = world.mm.join(1, created.code_b, 1, "private")
    assert "other side" in out.text


def test_one_group_cannot_serve_both_teams(world):
    for uid in (1, 2, 3):
        world.user(uid, f"P{uid}")
    created = world.mm.create_match(1, "2v2")
    world.mm.join(1, created.code_a, GROUP_A, "group")
    out = world.mm.join(3, created.code_b, GROUP_A, "group")
    assert "already linked to a different team" in out.text


def test_team_cannot_switch_groups(world):
    for uid in (1, 2):
        world.user(uid, f"P{uid}")
    created = world.mm.create_match(1, "2v2")
    world.mm.join(1, created.code_a, GROUP_A, "group")
    out = world.mm.join(2, created.code_a, -5555, "group")
    assert "already linked to a different group" in out.text


def test_group_join_requires_private_start(world):
    world.user(1, "Alice")
    world.user(7, "Lurker", private=False)
    created = world.mm.create_match(1, "2v2")
    out = world.mm.join(7, created.code_a, GROUP_A, "group", bot_username="EscapeBot")
    assert "press <b>Start</b>" in out.text
    assert out.buttons[0][0].url.startswith("https://t.me/EscapeBot")


def test_2v2_code_in_private_chat_is_redirected(world):
    world.user(1, "Alice")
    world.user(2, "Bob")
    created = world.mm.create_match(1, "2v2")
    out = world.mm.join(2, created.code_a, 2, "private", bot_username="EscapeBot")
    assert "team's" in out.text
    assert "startgroup=" + created.code_a in out.buttons[0][0].url


def test_1v1_code_in_group_rejected(world):
    world.user(1, "Alice")
    world.user(2, "Bob")
    created = world.mm.create_match(1, "1v1")
    out = world.mm.join(2, created.code_b, GROUP_A, "group")
    assert "private chat" in out.text


def test_team_capacity_enforced(world):
    for uid in (1, 2, 5):
        world.user(uid, f"P{uid}")
    created = world.mm.create_match(1, "2v2")
    world.mm.join(1, created.code_a, GROUP_A, "group")
    world.mm.join(2, created.code_a, GROUP_A, "group")
    out = world.mm.join(5, created.code_a, GROUP_A, "group")
    assert "already has two players" in out.text


def test_one_open_match_per_user(world):
    world.user(1, "Alice")
    world.mm.create_match(1, "1v1")
    with pytest.raises(GameError):
        world.mm.create_match(1, "2v2")


def test_join_codes_are_unique_and_random(world):
    codes = set()
    for uid in range(100, 140):
        world.user(uid, f"P{uid}")
        created = world.mm.create_match(uid, "2v2")
        codes |= {created.code_a, created.code_b}
    assert len(codes) == 80


def test_lobby_leave_and_cancel(world):
    world.user(1, "Alice")
    world.user(2, "Bob")
    created = world.mm.create_match(1, "2v2")
    world.mm.join(2, created.code_b, GROUP_B, "group")
    out = world.engine.confirm_quit(2, created.match_id)
    assert "left the lobby" in out.text
    assert world.team(created.match_id, "B")["chat_id"] is None
    out = world.engine.confirm_quit(1, created.match_id)
    assert "cancelled" in out.text
    assert world.match(created.match_id)["status"] == "cancelled"
    # the user is free to create again
    world.mm.create_match(1, "1v1")


def test_group_can_be_reused_after_match_ends(world):
    match_id = world.setup_2v2()
    world.engine.confirm_quit(1, match_id)
    assert world.match(match_id)["status"] == "completed"
    world.setup_2v2(ids=(1, 2, 3, 4))  # same users, same groups, new match
