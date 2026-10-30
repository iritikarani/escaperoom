"""Loading and validating the puzzle set from ``puzzles/puzzles.json``.

Puzzles are pure data: the engine never hard-codes any puzzle content.
When a match is created the current puzzle set is snapshotted into the
match row, so editing the JSON file later never changes a match that is
already in progress, and both sides are guaranteed identical content.
"""

from __future__ import annotations

import hashlib
import json
import logging
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

    return Puzzle(
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


def load_puzzles(path: str | Path, assets_dir: str | Path | None = None) -> PuzzleSet:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PuzzleError(f"Puzzle file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PuzzleError(f"Puzzle file is not valid JSON: {exc}") from exc
    return PuzzleSet.from_data(data, Path(assets_dir) if assets_dir else None)


class PuzzleProvider:
    """Reloads ``puzzles.json`` when it changes on disk, for new matches only."""

    def __init__(self, path: str | Path, assets_dir: str | Path | None = None):
        self.path = Path(path)
        self.assets_dir = assets_dir
        self._mtime: float | None = None
        self._current: PuzzleSet | None = None
        self.get()  # fail fast on a broken file

    def get(self) -> PuzzleSet:
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
