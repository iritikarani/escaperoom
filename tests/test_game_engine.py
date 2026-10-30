import messages as msg
from tests.conftest import ANSWERS, GROUP_A, GROUP_B


def room_texts(world, chat):
    return [t for t in world.texts(chat) if "/ 7</b>" in t]


# -- 1v1 ------------------------------------------------------------------------

def test_correct_answer_unlocks_next_room(world):
    match_id = world.setup_1v1()
    out = world.answer(1, 1, "private", ANSWERS[1])
    assert out.text is None and out.data["advanced_to"] == 2
    assert world.team(match_id, "A")["current_room"] == 2
    assert world.team(match_id, "B")["current_room"] == 1
    latest = room_texts(world, 1)[-1]
    assert "ROOM 2 UNLOCKED" in latest and "ROOM 2 / 7" in latest


def test_incorrect_answer_does_not_advance_or_reveal(world):
    match_id = world.setup_1v1()
    out = world.answer(1, 1, "private", "123")
    assert out.text in msg.WRONG_ANSWER_REPLIES
    assert ANSWERS[1] not in out.text
    assert world.team(match_id, "A")["current_room"] == 1
    assert world.db.query_one("SELECT COUNT(*) AS n FROM guess_attempts WHERE is_correct = 0")["n"] == 1


def test_case_whitespace_and_alternative_answers(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", [1])
    assert world.answer(1, 1, "private", "   A   WALL   CLOCK ").text is None
    assert world.answer(1, 1, "private", "Hidden-Door!").text is None
    assert world.team(match_id, "A")["current_room"] == 4


def test_rooms_unlock_in_order_and_future_answers_rejected(world):
    match_id = world.setup_1v1()
    # Answering Room 3 while in Room 1 must not work.
    assert world.answer(1, 1, "private", ANSWERS[3]).text in msg.WRONG_ANSWER_REPLIES
    assert world.team(match_id, "A")["current_room"] == 1
    world.solve(1, 1, "private", [1, 2, 3])
    rooms = [int(t.split("ROOM ")[-1].split(" / 7")[0]) for t in room_texts(world, 1)]
    assert rooms == [1, 2, 3, 4]


def test_future_puzzles_never_sent_early(world):
    world.setup_1v1()
    world.solve(1, 1, "private", [1])
    all_text = "\n".join(world.texts(1) + world.texts(2))
    for room in (3, 4, 5, 6, 7):
        assert f"ROOM {room} / 7" not in all_text


def test_opponent_progress_notification_reveals_only_room(world):
    world.setup_1v1()
    world.solve(1, 1, "private", [1, 2])
    alerts = [t for t in world.texts(2) if "Your opponent just reached" in t]
    assert alerts == [msg.opponent_progress(2, "1v1"), msg.opponent_progress(3, "1v1")]
    bob_text = "\n".join(world.texts(2))
    assert "ROOM 2 / 7" not in bob_text and "clock" not in bob_text.lower()


def test_wrong_guesses_not_shared_with_opponent(world):
    world.setup_1v1()
    before = len(world.outbox(2))
    world.answer(1, 1, "private", "my secret wrong guess")
    assert len(world.outbox(2)) == before


def test_full_game_winner_detection_and_result(world):
    match_id = world.setup_1v1()
    world.clock.advance(5)
    world.hints.request_hint(1, 1, "private")
    world.solve(2, 2, "private", [1, 2])
    world.clock.advance(18 * 60 + 37)
    world.solve(1, 1, "private", range(1, 8))
    match = world.match(match_id)
    assert match["status"] == "completed" and match["end_reason"] == "escaped"
    winner = world.team(match_id, "A")
    assert match["winner_team_id"] == winner["team_id"]
    result = world.db.query_one("SELECT * FROM match_results WHERE match_id = ?", (match_id,))
    assert result["result_type"] == "escaped"
    assert result["duration_seconds"] == 18 * 60 + 42
    assert result["winner_paid_hints_used"] == 1
    assert result["loser_room_reached"] == 3
    win = [t for t in world.texts(1) if "ESCAPE SUCCESSFUL" in t][0]
    assert "Alice1" in win and "18 minutes, 42 seconds" in win and "Paid hints used: 1" in win
    lose = [t for t in world.texts(2) if "MATCH OVER" in t][0]
    assert "Alice1" in lose and "Room 3 of 7" in lose


def test_no_answers_after_match_completed(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", range(1, 8))
    out = world.answer(2, 2, "private", ANSWERS[1])
    assert "over" in out.text
    assert world.team(match_id, "B")["current_room"] == 1


def test_duplicate_victory_impossible(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", range(1, 7))
    world.solve(2, 2, "private", range(1, 7))
    assert world.answer(1, 1, "private", ANSWERS[7]).text is None
    assert "over" in world.answer(2, 2, "private", ANSWERS[7]).text
    assert world.db.query_one("SELECT COUNT(*) AS n FROM match_results")["n"] == 1
    assert world.team(match_id, "B")["current_room"] == 7


def test_answer_rate_limit(world):
    world.settings.__dict__["answer_rate_limit_per_minute"] = 3  # frozen dataclass, test-only
    world.setup_1v1()
    for _ in range(3):
        world.answer(1, 1, "private", "0000")
    assert "Slow down" in world.answer(1, 1, "private", ANSWERS[1]).text
    world.clock.advance(61)
    assert world.answer(1, 1, "private", ANSWERS[1]).text is None


def test_private_text_routing(world):
    world.user(9, "Loner")
    assert "not in a match" in world.engine.handle_private_text(9, 9, "hello").text
    world.user(1, "A")
    world.mm.create_match(1, "1v1")
    assert "Waiting" in world.engine.handle_private_text(1, 1, "369").text


def test_status(world):
    match_id = world.setup_1v1()
    world.solve(1, 1, "private", [1, 2])
    world.hints.request_hint(2, 2, "private")
    world.clock.advance(65)
    text = world.engine.status(1, 1, "private").text
    assert match_id in text and "1v1" in text
    assert "Your room: <b>Room 3 / 7</b>" in text
    assert "Opponent room: <b>Room 1 / 7</b>" in text
    assert "Paid hints left: <b>3 / 3</b>" in text and "Free hints received: 1" in text
    assert "1 minute, 5 seconds" in text
    assert "369" not in text  # no puzzle content / answers


def test_lobby_status(world):
    world.user(1, "Alice")
    created = world.mm.create_match(1, "2v2")
    text = world.engine.status(1, 1, "private").text
    assert "Waiting for players" in text and "group not linked" in text and created.match_id in text


def test_forfeit_1v1(world):
    match_id = world.setup_1v1()
    out = world.engine.request_quit(2, 2, "private")
    assert out.buttons and out.buttons[0][0].callback == f"quit:yes:{match_id}:2"
    assert world.match(match_id)["status"] == "active"  # nothing happens before confirming
    out = world.engine.confirm_quit(2, match_id)
    assert "forfeited" in out.text
    match = world.match(match_id)
    assert match["end_reason"] == "forfeit"
    assert match["winner_team_id"] == world.team(match_id, "A")["team_id"]
    result = world.db.query_one("SELECT * FROM match_results WHERE match_id = ?", (match_id,))
    assert result["result_type"] == "forfeit" and result["forfeited_by"] == 2
    assert any("YOUR OPPONENT FORFEITED" in t for t in world.texts(1))
    assert "already over" in world.engine.confirm_quit(1, match_id).text


def test_outsider_cannot_quit_someone_elses_match(world):
    match_id = world.setup_1v1()
    world.user(9, "Mallory")
    assert "not part of that match" in world.engine.confirm_quit(9, match_id).text
    assert world.match(match_id)["status"] == "active"


# -- 2v2 ----------------------------------------------------------------------------

def test_either_teammate_can_answer_and_team_advances(world):
    match_id = world.setup_2v2()
    assert world.answer(2, GROUP_A, "supergroup", ANSWERS[1]).text is None
    assert world.answer(1, GROUP_A, "supergroup", ANSWERS[2]).text is None
    team = world.team(match_id, "A")
    assert team["current_room"] == 3
    # one shared progression for the whole team
    assert world.db.query_one("SELECT COUNT(*) AS n FROM teams WHERE match_id = ?", (match_id,))["n"] == 2
    latest = room_texts(world, GROUP_A)[-1]
    assert "ROOM 3 / 7" in latest and "solved by Ann1" in latest


def test_team_room_goes_to_the_shared_group(world):
    world.setup_2v2()
    world.solve(1, GROUP_A, "supergroup", [1])
    assert any("ROOM 2 / 7" in t for t in world.texts(GROUP_A))
    # nothing about it is sent to individual private chats
    assert not any("ROOM 2 / 7" in t for t in world.texts(1) + world.texts(2))


def test_only_one_advancement_per_room(world):
    match_id = world.setup_2v2()
    world.solve(1, GROUP_A, "supergroup", [1])
    # teammate submits the old room's answer a moment later
    out = world.answer(2, GROUP_A, "supergroup", ANSWERS[1])
    assert out.text in msg.WRONG_ANSWER_REPLIES
    assert world.team(match_id, "A")["current_room"] == 2
    assert world.db.query_one("SELECT COUNT(*) AS n FROM room_completions")["n"] == 1


def test_2v2_answers_must_be_in_group(world):
    world.setup_2v2()
    out = world.answer(1, 1, "private", ANSWERS[1])
    assert "team group" in out.text
    out = world.engine.handle_private_text(1, 1, ANSWERS[1])
    assert "/answer" in out.text


def test_opposing_team_cannot_act_in_other_group(world):
    match_id = world.setup_2v2()
    out = world.answer(3, GROUP_A, "supergroup", ANSWERS[1])
    assert "Only the registered members" in out.text
    out = world.hints.request_hint(3, GROUP_A, "supergroup")
    assert "Only the registered members" in out.text
    assert world.team(match_id, "A")["paid_hints_used"] == 0
    assert "Only the registered members" in world.engine.status(3, GROUP_A, "supergroup").text


def test_unlinked_group_cannot_play(world):
    world.setup_2v2()
    out = world.answer(1, -7777, "supergroup", ANSWERS[1])
    assert "isn't linked" in out.text


def test_team_privacy(world):
    world.setup_2v2()
    world.answer(1, GROUP_A, "supergroup", "secret wrong idea")
    world.solve(1, GROUP_A, "supergroup", [1, 2])
    world.hints.request_hint(2, GROUP_A, "supergroup")
    b_text = "\n".join(world.texts(GROUP_B) + world.texts(3) + world.texts(4))
    assert "secret wrong idea" not in b_text
    assert "ROOM 2 / 7" not in b_text and "ROOM 3 / 7" not in b_text
    hint_a = world.db.query_one(
        "SELECT payload FROM outbox WHERE chat_id = ? AND kind = 'paid_hint'", (GROUP_A,))["payload"]
    assert "Hint 1/3 for Room 3" in hint_a
    assert "Hint 1/3 for Room 3" not in b_text


def test_team_forfeit_rule(world):
    match_id = world.setup_2v2()
    world.engine.confirm_quit(2, match_id)
    match = world.match(match_id)
    assert match["winner_team_id"] == world.team(match_id, "B")["team_id"]
    loser_msg = [t for t in world.texts(GROUP_A) if "FORFEITED" in t][0]
    assert "whole team forfeits" in loser_msg
    assert any("YOUR OPPONENT FORFEITED" in t for t in world.texts(GROUP_B))
    # groups are released
    assert world.db.query_one("SELECT COUNT(*) AS n FROM linked_groups WHERE released_at IS NULL")["n"] == 0


def test_2v2_victory_lists_both_teammates(world):
    match_id = world.setup_2v2()
    world.solve(3, GROUP_B, "supergroup", range(1, 8))
    win = [t for t in world.texts(GROUP_B) if "ESCAPE SUCCESSFUL" in t][0]
    assert "<b>Team B (Cat3 &amp; Dan4)</b> have escaped!" in win
    assert any("MATCH OVER" in t for t in world.texts(GROUP_A))
    assert world.match(match_id)["winner_team_id"] == world.team(match_id, "B")["team_id"]


# -- multiple matches -----------------------------------------------------------------

def test_simultaneous_matches_are_independent(world):
    m1 = world.setup_1v1(1, 2)
    m2 = world.setup_2v2(ids=(11, 12, 13, 14), groups=(-2001, -2002))
    m3 = world.setup_1v1(21, 22)
    m4 = world.setup_2v2(ids=(31, 32, 33, 34), groups=(-3001, -3002))
    world.solve(1, 1, "private", [1, 2])
    world.hints.request_hint(11, -2001, "supergroup")
    world.solve(33, -3002, "supergroup", range(1, 8))
    assert world.team(m1, "A")["current_room"] == 3
    assert world.team(m2, "A")["paid_hints_used"] == 1 and world.team(m2, "B")["free_hints_received"] == 1
    for m in (m1, m3):
        assert world.team(m, "B")["free_hints_received"] == 0
    assert world.team(m3, "A")["current_room"] == 1
    assert world.match(m4)["status"] == "completed"
    for m in (m1, m2, m3):
        assert world.match(m)["status"] == "active"
    assert not any("ESCAPE" in t for t in world.texts(21) + world.texts(1))
