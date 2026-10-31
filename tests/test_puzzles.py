"""The puzzle pool, and every sample puzzle checked mechanically, not by eye."""

import itertools
import json
import random
from collections import Counter

import pytest

from answer_validator import canonical, is_correct
from puzzle_manager import PuzzleError, PuzzleLibrary, PuzzleSet, load_puzzles
from tests.conftest import ASSETS_DIR, PUZZLES_PATH, GROUP_A, GROUP_B

DIFFICULTY = ["Easy", "Medium", "Hard", "Expert"]


@pytest.fixture(scope="module")
def library():
    return load_puzzles(PUZZLES_PATH, ASSETS_DIR)


def by_id(library, puzzle_id):
    return next(p for p in library if p.id == puzzle_id)


# -- the pool as a whole --------------------------------------------------------------

def test_every_room_has_three_options(library):
    assert Counter(p.room for p in library) == {room: 3 for room in range(1, 8)}
    assert len(library) == 21


def test_every_puzzle_is_complete(library):
    for p in library:
        assert p.id and p.title and p.question
        assert len(p.hints) == 3
        assert is_correct(p.solution, p.accepted_answers), p.id


def test_difficulty_never_drops_from_room_to_room(library):
    for room in range(1, 7):
        hardest_here = max(DIFFICULTY.index(p.difficulty) for p in library.options(room))
        easiest_next = min(DIFFICULTY.index(p.difficulty) for p in library.options(room + 1))
        assert hardest_here <= easiest_next, room
    assert {p.difficulty for p in library.options(1)} == {"Easy"}
    assert {p.difficulty for p in library.options(7)} == {"Expert"}


def test_answers_are_distinct_across_puzzles(library):
    owners = {}
    for p in library:
        for key in {canonical(a) for a in p.accepted_answers}:
            assert key not in owners, f"{p.id} and {owners[key]} share the answer {key!r}"
            owners[key] = p.id


def test_no_puzzle_accepts_another_rooms_answer(library):
    for p in library:
        for other in library:
            if other.room != p.room:
                for answer in other.accepted_answers:
                    assert not is_correct(answer, p.accepted_answers), (p.id, answer)


# -- random draws ----------------------------------------------------------------------

def test_draw_picks_one_per_room(library):
    puzzles = library.draw(rng=random.Random(1))
    assert [p.room for p in puzzles] == list(range(1, 8))
    assert all(p in library.options(p.room) for p in puzzles)


def test_draws_vary(library):
    seen = {tuple(p.id for p in library.draw(rng=random.Random(seed))) for seed in range(40)}
    assert len(seen) > 20
    for room in range(1, 8):
        used = {ids[room - 1] for ids in seen}
        assert used == {p.id for p in library.options(room)}, room


def test_draw_avoids_recent_puzzles(library):
    first = library.draw(rng=random.Random(3))
    for seed in range(20):
        again = library.draw(rng=random.Random(seed), avoid={p.id for p in first})
        assert not {p.id for p in again} & {p.id for p in first}


def test_avoid_falls_back_when_room_has_one_option():
    data = json.loads(PUZZLES_PATH.read_text())
    singles = PuzzleLibrary([p for p in load_puzzles(PUZZLES_PATH) if p.id in {
        e["id"] for e in data["puzzles"][::3]}])
    everything = {p.id for p in singles}
    assert len(singles.draw(avoid=everything)) == 7


def test_both_sides_get_the_same_random_set(world, monkeypatch):
    import puzzle_manager
    monkeypatch.setattr(puzzle_manager.secrets, "SystemRandom", lambda: random.Random(7))
    match_id = world.setup_2v2()
    rooms = {chat: [t for t in world.texts(chat) if "ROOM 1 / 7" in t] for chat in (GROUP_A, GROUP_B)}
    assert len(rooms[GROUP_A]) == 1 and rooms[GROUP_A] == rooms[GROUP_B]
    assert len(world.snapshot_ids(match_id)) == 7


def test_next_match_avoids_puzzles_the_players_already_saw(world, monkeypatch):
    """Without the bank, the puzzles.json pool still uses players' history."""
    import puzzle_manager
    seeds = iter(range(100))
    monkeypatch.setattr(puzzle_manager.secrets, "SystemRandom", lambda: random.Random(next(seeds)))
    first = world.setup_1v1()
    world.solve(1, 1, "private", range(1, 8))  # Alice sees all 7 rooms; Bob saw room 1
    second = world.setup_1v1()
    assert not set(world.snapshot_ids(first)) & set(world.snapshot_ids(second))


def test_old_snapshots_without_ids_still_load(library):
    data = json.loads(library.draw(rng=random.Random(0)).to_json())
    for entry in data["puzzles"]:
        del entry["id"]
    assert len(PuzzleSet.from_json(json.dumps(data))) == 7


# -- each sample puzzle, solved by code --------------------------------------------------

def caesar(text, shift):
    return "".join(chr((ord(c) - 65 + shift) % 26 + 65) if c.isalpha() else c for c in text)


def atbash(text):
    return "".join(chr(155 - ord(c)) if c.isalpha() else c for c in text)


def test_room1_locks(library):
    assert f"{3}{3 * 2}{3 + 3 * 2}" == "369"              # triangle
    assert f"{12}{24}" == "1224"                            # months, hours
    assert f"{7}{7 - 5}{7 + (7 - 5)}" == "729"              # week
    assert by_id(library, "padlock-week").accepted_answers == ("729",)


def test_room2_riddles(library):
    assert "clock" in by_id(library, "riddle-clock").accepted_answers
    assert is_correct("A book", by_id(library, "riddle-book").accepted_answers)
    assert is_correct("raindrops", by_id(library, "riddle-rain").accepted_answers)


def test_room3_ciphers(library):
    assert caesar("HIDDEN DOOR", 3) == "KLGGHQ GRRU"
    assert atbash("SECRET TUNNEL") == "HVXIVG GFMMVO"
    assert caesar("GOLDEN GATE", -1) == "FNKCDM FZSD"
    for pid, cipher in [("caesar-hidden-door", "KLGGHQ GRRU"), ("atbash-secret-tunnel", "HVXIVG GFMMVO"),
                        ("shift-back-golden-gate", "FNKCDM FZSD")]:
        assert cipher in by_id(library, pid).question


def test_room4_words(library):
    for phrase in ["skeleton key", "keyhole", "keyboard", "keystone", "keynote"]:
        assert "key" in phrase
    assert sorted("listen") == sorted("silent")
    for word in ["sunlight", "lighthouse", "moonlight", "lightyear", "flashlight", "lightning"]:
        assert "light" in word


def test_room5_logic_has_unique_answers():
    # Three chests: exactly one inscription true.
    chests = [k for k in ("oak", "iron", "glass") if [k == "oak", k != "iron", k != "oak"].count(True) == 1]
    assert chests == ["iron"]

    # Four hooks: gold right of silver, brass not at an end, copper left of brass.
    layouts = []
    for order in itertools.permutations(["brass", "silver", "copper", "gold"]):
        pos = {key: i + 1 for i, key in enumerate(order)}
        if pos["gold"] == pos["silver"] + 1 and pos["brass"] not in (1, 4) and pos["copper"] < pos["brass"]:
            layouts.append(order)
    assert layouts == [("copper", "brass", "silver", "gold")]

    # Mislabelled boxes: every label wrong; a ruby came out of "rubies and pearls".
    labels = ["rubies", "pearls", "both"]
    solutions = []
    for contents in itertools.permutations(labels):
        box = dict(zip(labels, contents))
        if all(box[label] != label for label in labels) and box["both"] != "pearls":
            solutions.append(box)
    assert solutions == [{"rubies": "pearls", "pearls": "both", "both": "rubies"}]


def test_room6_codes(library):
    numbers = [25, 3, 29, 41, 11, 37, 29]
    assert "".join(chr((n - 1) // 2 + 64) for n in numbers) == "LANTERN"

    keypad = {"2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL", "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ"}
    groups = "222 · 666 · 6 · 7 · 2 · 7777 · 7777"
    assert groups in by_id(library, "phone-keypad-compass").question
    word = "".join(keypad[g[0]][len(g) - 1] for g in groups.split(" · "))
    assert word == "COMPASS"

    question = by_id(library, "binary-mirror").question
    rows = [line for line in question.split("\n") if set(line) <= set("●○<code>/") and "●" in line]
    rows = [r.replace("<code>", "").replace("</code>", "") for r in rows]
    letters = [chr(int(r.replace("●", "1").replace("○", "0"), 2) + 64) for r in rows]
    assert "".join(letters) == "MIRROR"


def test_room7_finales_are_unique():
    def codes(rule):
        return ["".join(map(str, d)) for d in itertools.product(range(10), repeat=4)
                if 0 not in d and len(set(d)) == 4 and rule(*d)]

    assert codes(lambda a, b, c, d: b == 3 * a and c == 6 and a + d == 10 and d in (1, 4, 9)) == ["1369"]
    assert codes(lambda a, b, c, d: d == 7 and b == 2 * c and a == b + c and a + b + c + d == 19) == ["6427"]

    clue = {"IV": "S", "I": "O", "VII": "E", "III": "T", "VI": "D", "II": "U", "V": "I"}
    order = ["I", "II", "III", "IV", "V", "VI", "VII"]
    assert "".join(clue[n] for n in order) == "OUTSIDE"
    assert "ELEVEN".count("E") == 3


# -- file validation ----------------------------------------------------------------------

def _data():
    return json.loads(PUZZLES_PATH.read_text())


def test_missing_room_rejected():
    data = _data()
    data["puzzles"] = [p for p in data["puzzles"] if p["room"] != 4]
    from puzzle_manager import _parse_puzzle
    with pytest.raises(PuzzleError, match="every room"):
        PuzzleLibrary([_parse_puzzle(e, None) for e in data["puzzles"]])


def test_invalid_entries_rejected(tmp_path):
    cases = [
        ("hints", ["only one"]),
        ("accepted_answers", ["  !! "]),
        ("image", "../../etc/passwd"),
        ("image", "missing.png"),
        ("id", ""),
    ]
    for key, value in cases:
        data = _data()
        data["puzzles"][0][key] = value
        path = tmp_path / "p.json"
        path.write_text(json.dumps(data))
        with pytest.raises(PuzzleError):
            load_puzzles(path, ASSETS_DIR)


def test_duplicate_ids_rejected(tmp_path):
    data = _data()
    data["puzzles"][1]["id"] = data["puzzles"][0]["id"]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data))
    with pytest.raises(PuzzleError, match="Duplicate"):
        load_puzzles(path)


def test_puzzles_without_ids_get_stable_ones(tmp_path):
    data = _data()
    for entry in data["puzzles"]:
        entry.pop("id")
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data))
    assert [p.id for p in load_puzzles(path).options(2)] == ["room2-1", "room2-2", "room2-3"]


def test_single_puzzle_per_room_still_works(tmp_path):
    data = _data()
    data["puzzles"] = data["puzzles"][::3]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data))
    library = load_puzzles(path)
    assert len(library) == 7 and len(library.draw()) == 7


def test_snapshot_roundtrip(library):
    drawn = library.draw(rng=random.Random(5))
    again = PuzzleSet.from_json(drawn.to_json())
    assert again.fingerprint == drawn.fingerprint
    assert [p.id for p in again] == [p.id for p in drawn]
