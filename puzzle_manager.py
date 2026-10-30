"""Loading and validating puzzles from ``puzzles/puzzles.json``.

Puzzles are pure data: the engine never hard-codes any puzzle content.

The file is a *pool*: it may hold several puzzles for the same room. When a
match is created, one puzzle per room is drawn at random (a
:class:`PuzzleSet`) and snapshotted into the match row. Both sides of a
match therefore always get the identical seven puzzles, editing the file
never changes a match in progress, and repeat games differ.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from answer_validator import canonical
from config import HINTS_PER_ROOM, TOTAL_ROOMS

log = logging.getLogger(__name__)


class PuzzleError(ValueError):
    """The puzzle file is malformed."""


@dataclass(frozen=True)
class Puzzle:
    id: str
    room: int
    title: str
    difficulty: str
    question: str
    accepted_answers: tuple[str, ...]
    hints: tuple[str, ...]
    solution: str
    image: str | None = None
    image_caption: str | None = None
    explanation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["accepted_answers"] = list(self.accepted_answers)
        data["hints"] = list(self.hints)
        return data


def _require_str(entry: dict, key: str, room: Any, optional: bool = False) -> str | None:
    value = entry.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PuzzleError(f"Room {room}: '{key}' must be a non-empty string")
    return value.strip()


def _parse_puzzle(entry: Any, assets_dir: Path | None) -> Puzzle:
    if not isinstance(entry, dict):
        raise PuzzleError("Every puzzle must be a JSON object")
    room = entry.get("room")
    if not isinstance(room, int) or isinstance(room, bool):
        raise PuzzleError(f"Puzzle has invalid room number: {room!r}")

    answers = entry.get("accepted_answers")
    if not isinstance(answers, list) or not answers:
        raise PuzzleError(f"Room {room}: 'accepted_answers' must be a non-empty list")
    for answer in answers:
        if not isinstance(answer, str) or not canonical(answer):
            raise PuzzleError(f"Room {room}: accepted answer {answer!r} is empty after normalisation")

    hints = entry.get("hints")
    if (
        not isinstance(hints, list)
        or len(hints) != HINTS_PER_ROOM
        or not all(isinstance(h, str) and h.strip() for h in hints)
    ):
        raise PuzzleError(f"Room {room}: 'hints' must contain exactly {HINTS_PER_ROOM} non-empty strings")

    image = _require_str(entry, "image", room, optional=True)
    if image is not None:
        if Path(image).is_absolute() or ".." in Path(image).parts:
            raise PuzzleError(f"Room {room}: image path must be relative to assets/puzzle_images")
        if assets_dir is not None and not (assets_dir / image).is_file():
            raise PuzzleError(f"Room {room}: image file not found: {assets_dir / image}")

    puzzle_id = entry.get("id")
    if puzzle_id is not None and (not isinstance(puzzle_id, str) or not puzzle_id.strip()):
        raise PuzzleError(f"Room {room}: 'id' must be a non-empty string")

    return Puzzle(
        id=(puzzle_id or "").strip(),
        room=room,
        title=_require_str(entry, "title", room),
        difficulty=_require_str(entry, "difficulty", room),
        question=_require_str(entry, "question", room),
        accepted_answers=tuple(a.strip() for a in answers),
        hints=tuple(h.strip() for h in hints),
        solution=_require_str(entry, "solution", room),
        image=image,
        image_caption=_require_str(entry, "image_caption", room, optional=True),
        explanation=_require_str(entry, "explanation", room, optional=True),
    )


class PuzzleSet:
    """An immutable, validated set of exactly ``TOTAL_ROOMS`` puzzles."""

    def __init__(self, puzzles: list[Puzzle]):
        rooms = sorted(p.room for p in puzzles)
        if rooms != list(range(1, TOTAL_ROOMS + 1)):
            raise PuzzleError(
                f"Puzzle set must contain rooms 1..{TOTAL_ROOMS} exactly once; got {rooms}"
            )
        self._by_room = {p.room: p for p in puzzles}

    def get(self, room: int) -> Puzzle:
        try:
            return self._by_room[room]
        except KeyError:
            raise PuzzleError(f"No puzzle for room {room}") from None

    def __iter__(self):
        return (self._by_room[r] for r in sorted(self._by_room))

    def __len__(self) -> int:
        return len(self._by_room)

    def to_json(self) -> str:
        return json.dumps({"puzzles": [p.to_dict() for p in self]}, ensure_ascii=False)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:12]

    @classmethod
    def from_data(cls, data: Any, assets_dir: Path | None = None) -> "PuzzleSet":
        entries = data.get("puzzles") if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise PuzzleError("Puzzle file must be a list or an object with a 'puzzles' list")
        return cls([_parse_puzzle(entry, assets_dir) for entry in entries])

    @classmethod
    def from_json(cls, text: str) -> "PuzzleSet":
        # Snapshots were validated when the match was created; image files
        # are checked again at send time.
        return cls.from_data(json.loads(text), assets_dir=None)


class PuzzleLibrary:
    """Every puzzle in the file, grouped by room (one or more per room)."""

    def __init__(self, puzzles: list[Puzzle]):
        by_room: dict[int, list[Puzzle]] = {}
        for puzzle in puzzles:
            by_room.setdefault(puzzle.room, []).append(puzzle)
        if sorted(by_room) != list(range(1, TOTAL_ROOMS + 1)):
            raise PuzzleError(
                f"The puzzle file needs at least one puzzle for every room 1..{TOTAL_ROOMS}; "
                f"found rooms {sorted(by_room)}"
            )
        # Give puzzles without an explicit id a stable one: room<N>-<position>.
        seen: set[str] = set()
        self._by_room: dict[int, tuple[Puzzle, ...]] = {}
        for room, entries in sorted(by_room.items()):
            named = []
            for position, puzzle in enumerate(entries, start=1):
                if not puzzle.id:
                    puzzle = Puzzle(**{**puzzle.__dict__, "id": f"room{room}-{position}"})
                if puzzle.id in seen:
                    raise PuzzleError(f"Duplicate puzzle id {puzzle.id!r}")
                seen.add(puzzle.id)
                named.append(puzzle)
            self._by_room[room] = tuple(named)

    def options(self, room: int) -> tuple[Puzzle, ...]:
        return self._by_room[room]

    def __iter__(self):
        return (p for room in sorted(self._by_room) for p in self._by_room[room])

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_room.values())

    def draw(self, rng=None, avoid: frozenset[str] | set[str] = frozenset()) -> PuzzleSet:
        """Pick one puzzle per room at random.

        Puzzles whose ids are in ``avoid`` (e.g. the ones a player had in
        their previous match) are skipped whenever the room has another option.
        """
        rng = rng or secrets.SystemRandom()
        chosen = []
        for room in sorted(self._by_room):
            options = self._by_room[room]
            fresh = [p for p in options if p.id not in avoid] or list(options)
            chosen.append(rng.choice(fresh))
        return PuzzleSet(chosen)


def load_puzzles(path: str | Path, assets_dir: str | Path | None = None) -> PuzzleLibrary:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PuzzleError(f"Puzzle file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PuzzleError(f"Puzzle file is not valid JSON: {exc}") from exc
    entries = data.get("puzzles") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise PuzzleError("Puzzle file must be a list or an object with a 'puzzles' list")
    assets = Path(assets_dir) if assets_dir else None
    return PuzzleLibrary([_parse_puzzle(entry, assets) for entry in entries])


class PuzzleProvider:
    """Reloads ``puzzles.json`` when it changes on disk, for new matches only."""

    def __init__(self, path: str | Path, assets_dir: str | Path | None = None):
        self.path = Path(path)
        self.assets_dir = assets_dir
        self._mtime: float | None = None
        self._current: PuzzleLibrary | None = None
        self.get()  # fail fast on a broken file

    def get(self) -> PuzzleLibrary:
        try:
            mtime = self.path.stat().st_mtime
            if self._current is None or mtime != self._mtime:
                self._current = load_puzzles(self.path, self.assets_dir)
                self._mtime = mtime
        except (OSError, PuzzleError):
            if self._current is None:
                raise
            # Keep serving the last valid set rather than breaking new matches.
            log.exception("Could not reload %s; keeping the previous puzzle set", self.path)
            self._mtime = None
        return self._current
