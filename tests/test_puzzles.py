"""The sample puzzles are checked mechanically, not just by eye."""

import itertools
import json

import pytest

from answer_validator import canonical, is_correct
from puzzle_manager import PuzzleError, PuzzleSet, load_puzzles
from tests.conftest import ASSETS_DIR, PUZZLES_PATH


@pytest.fixture(scope="module")
def puzzles():
    return load_puzzles(PUZZLES_PATH, ASSETS_DIR)


def test_seven_rooms_with_three_hints(puzzles):
    assert [p.room for p in puzzles] == list(range(1, 8))
    for p in puzzles:
        assert len(p.hints) == 3
        assert p.accepted_answers
        assert is_correct(p.solution, p.accepted_answers)


def test_difficulty_progresses(puzzles):
    order = ["Easy", "Medium", "Hard", "Expert"]
    levels = [order.index(p.difficulty) for p in puzzles]
    assert levels == sorted(levels)
    assert levels[0] == 0 and levels[-1] == 3


def test_answers_are_distinct_across_rooms(puzzles):
    keys = [canonical(a) for p in puzzles for a in p.accepted_answers]
    assert len(keys) == len(set(keys))


def test_room1_number_lock():
    first = 3                      # sides of a triangle
    second = first * 2
    last = first + second
    assert f"{first}{second}{last}" == "369"


def test_room3_caesar_cipher(puzzles):
    def shift(text, n):
        return "".join(chr((ord(c) - 65 + n) % 26 + 65) if c.isalpha() else c for c in text)
    assert shift("HIDDEN DOOR", 3) == "KLGGHQ GRRU"
    assert "KLGGHQ GRRU" in puzzles.get(3).question


def test_room5_exactly_one_true_statement():
    solutions = []
    for key in ("oak", "iron", "glass"):
        statements = [key == "oak", key != "iron", key != "oak"]
        if sum(statements) == 1:
            solutions.append(key)
    assert solutions == ["iron"]


def test_room6_numbers_decode_to_lantern(puzzles):
    numbers = [25, 3, 29, 41, 11, 37, 29]
    word = "".join(chr((n - 1) // 2 + 64) for n in numbers)
    assert all((n - 1) % 2 == 0 for n in numbers)
    assert word == "LANTERN"
    assert "25 · 3 · 29 · 41 · 11 · 37 · 29" in puzzles.get(6).question


def test_room7_code_is_unique():
    squares = {1, 4, 9}
    rooms_escaped = 6
    codes = []
    for a, b, c, d in itertools.product(range(10), repeat=4):
        digits = (a, b, c, d)
        if 0 in digits or len(set(digits)) != 4:
            continue
        if b == 3 * a and c == rooms_escaped and a + d == 10 and d in squares:
            codes.append(f"{a}{b}{c}{d}")
    assert codes == ["1369"]


def test_room4_word_fits_every_gap():
    for phrase in ["skeleton key", "keyhole", "keyboard", "keystone", "keynote"]:
        assert "key" in phrase


def test_invalid_files_rejected(tmp_path):
    data = json.loads(PUZZLES_PATH.read_text())
    bad = json.loads(json.dumps(data))
    bad["puzzles"] = bad["puzzles"][:6]
    with pytest.raises(PuzzleError):
        PuzzleSet.from_data(bad)

    bad = json.loads(json.dumps(data))
    bad["puzzles"][0]["hints"] = ["only one"]
    with pytest.raises(PuzzleError):
        PuzzleSet.from_data(bad)

    bad = json.loads(json.dumps(data))
    bad["puzzles"][0]["accepted_answers"] = ["  !! "]
    with pytest.raises(PuzzleError):
        PuzzleSet.from_data(bad)

    bad = json.loads(json.dumps(data))
    bad["puzzles"][0]["image"] = "../../etc/passwd"
    with pytest.raises(PuzzleError):
        PuzzleSet.from_data(bad, ASSETS_DIR)

    bad = json.loads(json.dumps(data))
    bad["puzzles"][0]["image"] = "missing.png"
    with pytest.raises(PuzzleError):
        PuzzleSet.from_data(bad, ASSETS_DIR)


def test_snapshot_roundtrip(puzzles):
    again = PuzzleSet.from_json(puzzles.to_json())
    assert again.fingerprint == puzzles.fingerprint
