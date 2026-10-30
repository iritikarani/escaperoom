import json
import re
from pathlib import Path

import messages as msg
from config import BASE_DIR
from tests.conftest import ANSWERS, GROUP_A, GROUP_B


def test_non_participant_cannot_answer_in_private(world):
    world.setup_1v1()
    world.user(9, "Mallory")
    out = world.answer(9, 9, "private", ANSWERS[1])
    assert "not in a match" in out.text


def test_user_cannot_answer_for_another_side_by_chat_id(world):
    match_id = world.setup_1v1()
    # Bob tries to submit into Alice's private chat id - private context always
    # resolves from the *sender*, so this advances Bob (if correct), never Alice.
    world.answer(2, 1, "private", ANSWERS[1])
    assert world.team(match_id, "A")["current_room"] == 1
    assert world.team(match_id, "B")["current_room"] == 2


def test_user_messages_are_html_escaped(world):
    world.user(1, "<b>Evil</b>")
    world.user(2, "Bob & Co")
    created = world.mm.create_match(1, "1v1")
    world.mm.join(2, created.code_b, 2, "private")
    start = [t for t in world.texts(2) if "BEGUN" in t][0]
    assert "&lt;b&gt;Evil&lt;/b&gt;" in start and "<b>Evil</b>" not in start
    assert "Bob &amp; Co" in [t for t in world.texts(1) if "BEGUN" in t][0]


def test_sql_injection_in_inputs_is_inert(world):
    match_id = world.setup_1v1()
    out = world.answer(1, 1, "private", "'; DROP TABLE matches; --")
    assert out.text in msg.WRONG_ANSWER_REPLIES
    assert world.match(match_id) is not None
    assert "❌" in world.mm.join(1, "'; DROP TABLE teams; --", 1, "private").text


def test_status_never_exposes_chat_ids_or_answers(world):
    match_id = world.setup_2v2()
    for uid, chat in ((1, GROUP_A), (3, GROUP_B), (1, 1)):
        text = world.engine.status(uid, chat, "private" if chat > 0 else "supergroup").text
        assert str(GROUP_A) not in text and str(GROUP_B) not in text
        for answer in ANSWERS.values():
            assert answer not in re.sub(r"<code>.*?</code>", "", text)
    assert match_id


def test_messages_never_contain_solutions_before_solving(world):
    world.setup_2v2()
    world.solve(1, GROUP_A, "supergroup", [1, 2, 3])
    puzzles = json.loads((BASE_DIR / "puzzles" / "puzzles.json").read_text())["puzzles"]
    unsolved = {p["room"]: p["explanation"] for p in puzzles if p["room"] >= 4}
    every = "\n".join(m["text"] for m in world.outbox())
    for explanation in unsolved.values():
        assert explanation not in every


def test_wrong_answer_never_reveals_solution(world):
    world.setup_1v1()
    for room_answer in ANSWERS.values():
        out = world.answer(1, 1, "private", "definitely wrong")
        assert room_answer not in out.text


def test_token_not_hardcoded():
    pattern = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
    for path in Path(BASE_DIR).rglob("*"):
        if path.suffix in {".py", ".json", ".md", ".example", ".txt"} and "tests" not in path.parts \
                and ".git" not in path.parts:
            assert not pattern.search(path.read_text(errors="ignore")), path


def test_env_is_gitignored():
    ignored = (BASE_DIR / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
    assert any(line.startswith("data/") or line == "*.db" for line in ignored)


def test_quit_callback_carries_owner(world):
    match_id = world.setup_2v2()
    out = world.engine.request_quit(1, GROUP_A, "supergroup")
    assert out.buttons[0][0].callback == f"quit:yes:{match_id}:1"
    assert len(out.buttons[0][0].callback.encode()) <= 64  # Telegram callback_data limit
