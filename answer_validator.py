"""Answer normalisation and comparison.

Matching is *exact* after normalisation. There is deliberately no fuzzy or
edit-distance matching: a lock either opens or it doesn't, and fuzzy rules
risk accepting answers that are genuinely different.

Normalisation steps (applied to both the guess and every accepted answer):

1. Unicode NFKC, case-folding, accents removed (``Café`` -> ``cafe``).
2. Apostrophes are dropped (``don't`` -> ``dont``); every other punctuation
   character becomes a space (``blue-key!`` -> ``blue key``).
3. Whitespace is collapsed and trimmed.
4. One leading article (``a``/``an``/``the``) is ignored when followed by
   more words (``The Iron Chest`` == ``iron chest``).
5. Spaces are ignored for the final comparison (``1 369`` == ``1369``,
   ``bluekey`` == ``blue key``) - the letters/digits must still match exactly
   and in order.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from config import MAX_ANSWER_LENGTH

_APOSTROPHES = {ord(c): None for c in "'’‘ʼ`´"}
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the) (?=\S)")


def normalize(text: str) -> str:
    """Return the human-readable normalised form of ``text``."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(_APOSTROPHES)
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def canonical(text: str) -> str:
    """Return the comparison key for ``text``."""
    norm = _LEADING_ARTICLE.sub("", normalize(text))
    return norm.replace(" ", "")


def validate_guess(raw: str | None) -> tuple[bool, str | None]:
    """Check that a guess is usable. Returns ``(ok, error_message)``."""
    if raw is None or not raw.strip():
        return False, "✏️ Please include your answer, e.g. <code>/answer blue key</code>."
    if len(raw) > MAX_ANSWER_LENGTH:
        return False, f"✏️ That answer is too long (max {MAX_ANSWER_LENGTH} characters)."
    if not canonical(raw):
        return False, "✏️ That answer doesn't contain any letters or numbers."
    return True, None


def is_correct(guess: str, accepted_answers: Iterable[str]) -> bool:
    key = canonical(guess)
    if not key:
        return False
    return any(key == canonical(answer) for answer in accepted_answers)
