"""Procedural generator for the puzzle bank: 10,000 sets x 7 puzzles.

Run ``python puzzle_generator.py`` to (re)build ``puzzles/bank/bank.jsonl.gz``.
The output is deterministic for a given ``BANK_SEED``, so the committed file
can always be reproduced and checked.

Each set has one puzzle per position, and position N is always used for
Room N, keeping the game's difficulty curve when puzzles from different sets
are mixed:

    1 Easy    number locks        (fact locks, arithmetic locks)
    2 Easy    number patterns     (six kinds of sequence)
    3 Medium  ciphers             (Caesar, Atbash, mapped shifts)
    4 Medium  word puzzles        (first-letter, numbered and last-letter acrostics)
    5 Hard    logic               (ordering puzzles with a unique solution)
    6 Hard    code breaking       (affine A1Z26, reverse alphabet, binary lamps, phone keypad)
    7 Expert  final locks         (multi-clue keypads, scattered letters)

Every puzzle is verified when it is generated: ciphers and codes are decoded
back to their answer, and logic puzzles are brute-forced to prove exactly
one solution. Each record keeps a ``meta`` field with the parameters needed
to re-verify it (``verify_record``); players never see it.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import random
import string
from pathlib import Path

from answer_validator import canonical, is_correct

BANK_SEED = "escape-room-bank-v1"
SET_COUNT = 10_000
BANK_PATH = Path(__file__).resolve().parent / "puzzles" / "bank" / "bank.jsonl.gz"

DIFFICULTY = {1: "Easy", 2: "Easy", 3: "Medium", 4: "Medium", 5: "Hard", 6: "Hard", 7: "Expert"}
CATEGORY = {1: "number-lock", 2: "number-pattern", 3: "cipher", 4: "word-puzzle",
            5: "logic", 6: "code-breaking", 7: "final-lock"}
ALPHABET = string.ascii_uppercase
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh"]
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"]

# -- word lists ------------------------------------------------------------------

FACTS = [
    (1, "wheels on a unicycle"), (2, "wheels on a bicycle"), (3, "wheels on a tricycle"),
    (3, "sides of a triangle"), (4, "sides of a square"), (4, "legs on a dog"),
    (4, "wheels on a car"), (4, "seasons in a year"), (5, "sides of a pentagon"),
    (5, "vowels in the English alphabet"), (6, "sides of a hexagon"), (6, "legs on an insect"),
    (6, "faces on a dice"), (7, "days in a week"), (7, "colours in a rainbow"),
    (8, "legs on a spider"), (8, "sides of an octagon"), (8, "arms on an octopus"),
    (2, "eyes on a human face"), (0, "corners on a circle"),
]
LOCKS = ["rusty padlock", "bicycle lock", "brass combination lock", "wall safe", "steel keypad",
         "lockbox on the desk", "padlock on a wooden chest", "dial lock on the cabinet"]

ADJECTIVES = [
    "SILVER", "GOLDEN", "HIDDEN", "SECRET", "BROKEN", "ANCIENT", "FROZEN", "SILENT", "CRIMSON",
    "HOLLOW", "MISTY", "COPPER", "DUSTY", "WOODEN", "MARBLE", "IRON", "VELVET", "MIDNIGHT",
    "STONE", "CRYSTAL", "PAPER", "GLASS", "BRASS", "EMERALD", "SCARLET", "AMBER", "SHADOW",
    "TWISTED", "SLEEPING", "ROYAL", "FORGOTTEN", "LOCKED", "LOST", "SUNKEN", "PAINTED",
    "BURNING", "WHISPERING", "CLOCKWORK", "LUNAR", "STORMY", "QUIET", "RUSTY",
    "OPEN", "NARROW", "SPIRAL", "MOSSY", "PALE", "WILD", "GRAND",
]
NOUNS = [
    "KEY", "DOOR", "TUNNEL", "LANTERN", "CLOCK", "MIRROR", "GATE", "STAIRS", "CANDLE", "MAP",
    "COMPASS", "LADDER", "VAULT", "ATTIC", "CELLAR", "GARDEN", "LIBRARY", "CHEST", "BRIDGE",
    "TOWER", "WINDOW", "HALLWAY", "PASSAGE", "ROSE", "RAVEN", "OWL", "CROWN", "SWORD", "SHIELD",
    "ANCHOR", "BELL", "FEATHER", "SCROLL", "POTION", "RING", "THRONE", "FOUNTAIN", "STATUE",
    "TRAIN", "SHIP", "HARBOUR", "MILL", "FOREST", "RIVER", "ISLAND", "CASTLE", "CAVE", "WELL",
    "ORCHARD", "CHAPEL", "BEACON", "PENDULUM", "GLOBE", "TELESCOPE", "CABIN", "TRUNK", "DIARY",
    "LOCKET", "QUILL", "PORTRAIT",
]
WORDS = [
    "ANCHOR", "ARROW", "ATTIC", "BADGE", "BANNER", "BARREL", "BASKET", "BEACON", "BOTTLE",
    "BRIDGE", "BUCKET", "BUTTON", "CABIN", "CANDLE", "CANNON", "CANVAS", "CASTLE", "CELLAR",
    "CHAIN", "CHALK", "CHEST", "CIPHER", "CLOCK", "CLOUD", "COMET", "COMPASS", "COPPER", "CRADLE",
    "CROWN", "CRYSTAL", "DAGGER", "DESERT", "DIARY", "DRAGON", "DRUM", "EAGLE", "EMBER", "FALCON",
    "FEATHER", "FLAME", "FOREST", "FOSSIL", "FROST", "GARDEN", "GLOBE", "GOBLET", "HAMMER",
    "HARBOR", "HELMET", "ISLAND", "JACKET", "JEWEL", "JUNGLE", "KETTLE", "LADDER", "LANTERN",
    "LEMON", "LOCKET", "MAGNET", "MARBLE", "MEADOW", "MIRROR", "MONKEY", "NEEDLE", "OCEAN",
    "ORCHID", "PARROT", "PEBBLE", "PENCIL", "PEPPER", "PIRATE", "PLANET", "POCKET", "POTION",
    "PUZZLE", "QUILL", "RABBIT", "RAVEN", "RIBBON", "RIDDLE", "ROCKET", "SADDLE", "SAILOR",
    "SCROLL", "SHADOW", "SHIELD", "SILVER", "SPIDER", "STATUE", "SUMMIT", "SWORD", "TEMPLE",
    "THRONE", "TICKET", "TIGER", "TORCH", "TOWER", "TRAIN", "TUNNEL", "TURTLE", "VALLEY",
    "VELVET", "VIOLIN", "WAGON", "WALRUS", "WINDOW", "WIZARD", "ZIPPER", "APPLE", "ACORN",
    "BEETLE", "CACTUS", "DONKEY", "FLUTE", "GHOST", "HONEY", "IGLOO", "JIGSAW", "KNIGHT",
    "LIZARD", "MAPLE", "NUTMEG", "OYSTER", "PALACE", "QUARTZ", "RUBY", "SPHINX", "TROPHY",
    "UNICORN", "VOLCANO", "WHISTLE", "YACHT", "ZEBRA", "BALLOON", "CARPET", "DOLPHIN", "ENGINE",
    "FIDDLE", "GRAVEL", "HERMIT", "INSECT", "JASMINE", "KITTEN", "LOBSTER", "MUFFIN", "NAPKIN",
    "ORANGE", "PEACOCK", "RAFFLE", "SPROUT", "TEAPOT", "UMBRELLA", "VASE", "WALNUT",
]
WORDS = [w for w in dict.fromkeys(WORDS) if 4 <= len(w) <= 8]

CODE_WORDS = sorted({w for w in WORDS + NOUNS + ADJECTIVES if w.isalpha() and 4 <= len(w) <= 8})

OBJECTS = [
    "ANCHOR", "APPLE", "ARROW", "AXE", "BELL", "BOOK", "BOTTLE", "BUCKET", "CANDLE", "CLOCK",
    "COMB", "CUP", "DICE", "DRUM", "DUCK", "EGG", "ENVELOPE", "EASEL", "FAN", "FEATHER", "FORK",
    "FLUTE", "GLOBE", "GLOVE", "GUITAR", "HAT", "HAMMER", "HARP", "IGLOO", "INKPOT", "IRON",
    "JAR", "JUG", "JACKET", "KEY", "KETTLE", "KITE", "LAMP", "LADDER", "LEMON", "MAP", "MASK",
    "MUG", "NAIL", "NEEDLE", "NET", "OAR", "ONION", "OWL", "PEN", "PEAR", "PIANO", "QUILL",
    "QUILT", "QUEEN", "RING", "ROPE", "RULER", "SHOE", "SPOON", "SCARF", "TEAPOT", "TORCH",
    "TRUMPET", "UMBRELLA", "URN", "UKULELE", "VASE", "VIOLIN", "VEST", "WATCH", "WHEEL",
    "WHISTLE", "XYLOPHONE", "YARN", "YOYO", "YACHT", "ZIPPER", "ZEBRA", "ACORN", "BROOM",
    "CANOE", "DOLL", "EMERALD", "FLAG", "GOBLET", "HELMET", "JIGSAW", "KNIFE", "LOCKET",
    "MIRROR", "NAPKIN", "ORANGE", "PILLOW", "RADIO", "SOCK", "TICKET", "VALVE", "WALLET",
    "BRUSH", "CRAB", "DISC", "FISH", "HORN", "STAR", "BOX", "CLAW", "SAW", "BOW", "RADISH",
    "PIZZA", "TOOTHBRUSH", "BADGE", "SPONGE", "COMPASS", "PEACH", "CHAIR", "ARCH", "TOOTH",
    "CLAM", "DRAWER", "ANVIL", "PENCIL", "STOOL", "RIBBON", "CROWN", "COIN", "FAUCET", "BOAT",
    "SKATE", "CHALK", "PLANK", "BRICK", "BOOMERANG", "RING", "SPRING", "PAINTBRUSH", "HOURGLASS",
    "BUTTON", "CANNON", "CRAYON", "BASKET", "CARPET", "HELMET", "TROPHY", "DAISY", "PUPPY",
    "BELT", "BOOT", "CART", "SALT", "ROBOT", "PARROT", "BUCKET", "CIRCUIT", "FROG", "MUG",
    "BAG", "PIG", "RUG", "PLUG", "HONEY", "MONEY", "KEY", "TRAY", "TOY", "SHELL", "BALL",
    "OWL", "WHEEL", "STOOL", "PENCIL", "EAGLE", "PUZZLE", "CANDLE", "BICYCLE", "KITE", "CAPE",
]
OBJECTS = sorted(set(OBJECTS))
FIRST_LETTER = {c: [o for o in OBJECTS if o[0] == c] for c in ALPHABET}
LAST_LETTER = {c: [o for o in OBJECTS if o[-1] == c] for c in ALPHABET}

ORDER_THEMES = [
    ("key", "hook", ["BRASS", "SILVER", "COPPER", "GOLD", "IRON", "BRONZE", "STEEL"]),
    ("candle", "holder", ["RED", "BLUE", "GREEN", "WHITE", "BLACK", "YELLOW", "PURPLE"]),
    ("book", "shelf slot", ["ATLAS", "DIARY", "NOVEL", "LEDGER", "BIBLE", "DICTIONARY", "ALMANAC"]),
    ("potion", "rack slot", ["AMBER", "VIOLET", "CRIMSON", "EMERALD", "AZURE", "GOLDEN", "SILVER"]),
    ("statue", "plinth", ["OWL", "LION", "FOX", "BEAR", "WOLF", "STAG", "HAWK"]),
    ("painting", "nail", ["SUNSET", "HARBOUR", "FOREST", "PORTRAIT", "MOUNTAIN", "MEADOW", "STORM"]),
    ("jar", "shelf spot", ["HONEY", "SALT", "PEPPER", "TEA", "SUGAR", "RICE", "FLOUR"]),
]
KEYPAD = {"2": "ABC", "3": "DEF", "4": "GHI", "5": "JKL", "6": "MNO", "7": "PQRS", "8": "TUV", "9": "WXYZ"}
LETTER_KEY = {ch: (key, i + 1) for key, letters in KEYPAD.items() for i, ch in enumerate(letters)}

TITLES = {
    "fact_lock": ["The Rusty Padlock", "The Counting Lock", "The Bicycle Chain", "The Old Safe",
                  "The Number Wheels", "The Note on the Lock", "The Fact Lock", "The Chest Dial"],
    "arith_lock": ["The Chain of Digits", "The Clockmaker's Lock", "The Step-by-Step Lock",
                   "The Ledger Lock", "The Tumbler Puzzle", "The Calculating Door"],
    "sequence": ["The Glowing Numbers", "The Counting Wall", "The Number Strip", "The Tally Marks",
                 "The Pattern on the Floor", "The Missing Tile", "The Ticking Sequence"],
    "cipher": ["The Mysterious Cipher", "The Scratched Door", "The Coded Note", "The Mirror Alphabet",
               "The Shifted Letters", "The Painted Message", "The Brass Plate"],
    "acrostic": ["The Shelf of Clues", "The Labelled Jars", "The Cluttered Desk", "The Toy Box",
                 "The Lost Property Box", "The Workshop Wall", "The Market Stall"],
    "ordering": ["The Row of Clues", "The Careful Arrangement", "The Order of Things",
                 "The Shelf Riddle", "The Lineup", "The Collector's Puzzle"],
    "code:affine": ["The Lantern-Maker's Ledger", "The Accountant's Secret", "The Circled Numbers"],
    "code:reverse": ["The Number Scroll", "The Inverted Alphabet", "The Backwards Count"],
    "code:binary": ["The Blinking Lights", "The Signal Lamps", "The Lighthouse Code"],
    "code:keypad": ["The Old Telephone", "The Last Text Message", "The Dusty Mobile"],
    "keypad": ["The Vault Door", "The Escape Hatch", "The Final Keypad", "The Last Lock",
               "The Exit Code", "The Clockmaker's Vault"],
    "scatter": ["The Scattered Letters", "The Broken Sign", "The Shuffled Exit", "The Letter Hunt",
                "The Fallen Tiles", "The Final Word"],
}


def _title(rng: random.Random, family: str) -> str:
    return rng.choice(TITLES[family])


def _record(position, family, title, question, answers, hints, solution, explanation, meta):
    return {
        "position": position, "difficulty": DIFFICULTY[position], "category": CATEGORY[position],
        "family": family, "title": title, "question": question, "accepted_answers": answers,
        "hints": hints, "solution": solution, "explanation": explanation, "meta": meta,
    }


# -- position 1: number locks ------------------------------------------------------

def gen_fact_lock(rng):
    n = rng.choice([3, 4])
    facts = [rng.choice(FACTS) for _ in range(n)]
    if len({f[1] for f in facts}) < n:
        return None
    lock = rng.choice(LOCKS)
    code = "".join(str(v) for v, _ in facts)
    lines = "\n".join(f"• The {ORDINALS[i]} digit is the number of {phrase}." for i, (_, phrase) in enumerate(facts))
    q = f"A {lock} blocks the way. It needs a {n}-digit code, and a note is taped beside it:\n\n{lines}\n\nWhat is the code?"
    hints = ["Each line gives you one digit, in order.",
             f"The first digit is {facts[0][0]}: there are {facts[0][0]} {facts[0][1]}.",
             f"The code starts with {code[:-1]} - you only need the last digit."]
    return _record(1, "fact_lock", _title(rng, "fact_lock"), q, [code], hints, code,
                   "; ".join(f"{p} = {v}" for v, p in facts), {"facts": [p for _, p in facts]})


def gen_arith_lock(rng):
    first = rng.randint(1, 9)
    digits, steps = [first], []
    for _ in range(3):
        prev = digits[-1]
        options = []
        if prev <= 4:
            options.append(("double", prev * 2))
        if prev % 2 == 0 and prev:
            options.append(("half", prev // 2))
        for k in range(1, 6):
            if prev + k <= 9:
                options.append((f"plus:{k}", prev + k))
            if prev - k >= 0:
                options.append((f"minus:{k}", prev - k))
        op, val = rng.choice(options)
        steps.append(op)
        digits.append(val)
    words = {"double": "double the digit before it", "half": "half of the digit before it"}
    lines = [f"• The first digit is {first}."]
    for i, op in enumerate(steps, start=1):
        if op in words:
            text = words[op]
        else:
            kind, k = op.split(":")
            text = f"the digit before it {'plus' if kind == 'plus' else 'minus'} {k}"
        lines.append(f"• The {ORDINALS[i]} digit is {text}.")
    code = "".join(map(str, digits))
    q = (f"The {rng.choice(LOCKS)} needs a 4-digit code. Scratched beside it:\n\n"
         + "\n".join(lines) + "\n\nWhat is the 4-digit code?")
    hints = ["Work out each digit from the one before it.",
             f"The first two digits are {digits[0]} and {digits[1]}.",
             f"The code starts with {code[:3]}."]
    return _record(1, "arith_lock", _title(rng, "arith_lock"), q, [code], hints, code,
                   " -> ".join(map(str, digits)), {"first": first, "steps": steps})


# -- position 2: number patterns ------------------------------------------------------

def gen_sequence(rng):
    kind = rng.choice(["arith", "desc", "geom", "alt", "square", "fib"])
    if kind == "arith":
        a, d = rng.randint(1, 99), rng.randint(2, 25)
        terms, nxt = [a + i * d for i in range(5)], a + 5 * d
        rule, h1 = f"add {d} each time", "Look at the gap between neighbouring numbers."
    elif kind == "desc":
        d = rng.randint(3, 15)
        a = rng.randint(6 * d, 6 * d + 120)
        terms, nxt = [a - i * d for i in range(5)], a - 5 * d
        rule, h1 = f"subtract {d} each time", "The numbers are getting smaller by the same amount."
    elif kind == "geom":
        r = rng.choice([2, 3])
        a = rng.randint(1, 20 if r == 2 else 8)
        terms, nxt = [a * r ** i for i in range(5)], a * r ** 5
        rule, h1 = f"multiply by {r} each time", "The gaps keep growing - try dividing instead of subtracting."
    elif kind == "alt":
        p, q = rng.sample(range(1, 16), 2)
        a = rng.randint(1, 60)
        terms = [a]
        for i in range(5):
            terms.append(terms[-1] + (p if i % 2 == 0 else q))
        nxt = terms[-1] + p
        rule, h1 = f"add {p}, then {q}, then {p}, then {q}...", "There are two different gaps that take turns."
    elif kind == "square":
        n, c = rng.randint(1, 15), rng.randint(0, 40)
        terms, nxt = [(n + i) ** 2 + c for i in range(5)], (n + 5) ** 2 + c
        rule = f"square numbers ({n}x{n}, {n + 1}x{n + 1}, ...)" + (f" plus {c}" if c else "")
        h1 = "The gaps go up by 2 every time."
    else:
        a, b = rng.randint(1, 15), rng.randint(1, 20)
        terms = [a, b]
        for _ in range(4):
            terms.append(terms[-1] + terms[-2])
        nxt = terms[-1] + terms[-2]
        rule, h1 = "each number is the sum of the two before it", "Compare each number with the two just before it."
    shown = ", ".join(map(str, terms))
    place = rng.choice(["glow on the wall", "are carved into the floor tiles", "flicker on an old screen",
                        "are chalked on a blackboard", "are painted on the steps"])
    q = f"These numbers {place}, one after another:\n\n<b>{shown}, ?</b>\n\nWhat number comes next?"
    hints = [h1, f"The rule is: {rule}.", f"Apply the rule to {terms[-1]}" + (f" and {terms[-2]}." if kind == "fib" else ".")]
    return _record(2, "sequence", _title(rng, "sequence"), q, [str(nxt)], hints, str(nxt),
                   f"Rule: {rule}.", {"kind": kind, "terms": terms, "next": nxt})


# -- position 3: ciphers --------------------------------------------------------------

def _shift(text, k):
    return "".join(ALPHABET[(ALPHABET.index(c) + k) % 26] if c in ALPHABET else c for c in text)


def _atbash(text):
    return "".join(ALPHABET[25 - ALPHABET.index(c)] if c in ALPHABET else c for c in text)


def gen_cipher(rng):
    phrase = f"{rng.choice(ADJECTIVES)} {rng.choice(NOUNS)}"
    kind = rng.choice(["caesar", "caesar", "atbash"])
    surface = rng.choice(["Scratched into the door", "Painted across the wall", "Written on a scrap of paper",
                          "Engraved on a brass plate", "Chalked on the floor"])
    if kind == "atbash":
        coded = _atbash(phrase)
        q = (f"{surface}:\n\n<b>{coded}</b>\n\nA small diagram beside it shows <b>A ↔ Z, B ↔ Y, C ↔ X ...</b>\n\n"
             "Decode the two words to find the password.")
        hints = ["The alphabet has been folded in half: the first letter swaps with the last.",
                 "Every letter swaps with its mirror: A↔Z, B↔Y, M↔N.",
                 f"The first word is {phrase.split()[0]}."]
        meta = {"kind": "atbash", "coded": coded}
        explanation = f"Atbash: {coded} -> {phrase}."
    else:
        k = rng.randint(1, 25)
        coded = _shift(phrase, k)
        clue = rng.choice(ALPHABET)
        q = (f"{surface}:\n\n<b>{coded}</b>\n\nUnderneath, a tiny reminder: <b>{clue} → {_shift(clue, k)}</b>\n\n"
             "Decode the two words. They are the password.")
        hints = ["Every letter was replaced by another letter the same distance along the alphabet.",
                 f"{clue} became {_shift(clue, k)}, so every letter moved {k} places forward. Move each one {k} back.",
                 f"The first word is {phrase.split()[0]}."]
        meta = {"kind": "caesar", "coded": coded, "shift": k, "clue": clue}
        explanation = f"Shift of {k}: {coded} -> {phrase}."
    return _record(3, "cipher", _title(rng, "cipher"), q, [phrase.lower()], hints, phrase, explanation, meta)


# -- position 4: word puzzles ------------------------------------------------------

def gen_acrostic(rng):
    kind = rng.choice(["first", "numbered", "last"])
    index = LAST_LETTER if kind == "last" else FIRST_LETTER
    candidates = [w for w in WORDS if all(index[c] for c in w)]
    target = rng.choice(candidates)
    used, items = set(), []
    for c in target:
        choices = [o for o in index[c] if o not in used]
        if not choices:
            return None
        pick = rng.choice(choices)
        used.add(pick)
        items.append(pick)
    place = rng.choice(["on a dusty shelf", "in a toy box", "on the workbench", "in a glass cabinet",
                        "in the lost property box", "on a market stall"])
    if kind == "numbered":
        order = list(range(1, len(items) + 1))
        tagged = list(zip(order, items))
        rng.shuffle(tagged)
        listing = "\n".join(f"• {name.title()} (tag {n})" for n, name in tagged)
        q = (f"You find {len(items)} objects {place}, each with a numbered tag:\n\n{listing}\n\n"
             "Put the objects in tag order and read the <b>first letters</b>. What word do they spell?")
        hints = ["Sort the objects by their tag numbers, starting with tag 1.",
                 "Now take the first letter of each object's name.",
                 f"The word starts with {target[:2]}."]
        meta = {"kind": kind, "items": [[n, name] for n, name in tagged]}
    else:
        listing = "\n".join(f"{i}. {name.title()}" for i, name in enumerate(items, start=1))
        which = "first" if kind == "first" else "last"
        q = (f"You find {len(items)} objects lined up {place}, in this order:\n\n{listing}\n\n"
             f"Read the <b>{which} letter</b> of each object's name, top to bottom. What word do they spell?")
        hints = [f"Look only at the {which} letter of each word.",
                 f"The first object is a {items[0].title()}, so the word starts with {target[0]}.",
                 f"The word starts with {target[:3]}."]
        meta = {"kind": kind, "items": items}
    return _record(4, "acrostic", _title(rng, "acrostic"), q, [target.lower()], hints, target,
                   f"The letters spell {target}.", meta)


# -- position 5: ordering logic --------------------------------------------------------

def _order_clues(truth):
    """Every true clue about ``truth`` (a tuple of item names by position)."""
    n, pos = len(truth), {item: i for i, item in enumerate(truth)}
    clues = []
    for x, y in itertools.permutations(truth, 2):
        if pos[x] < pos[y]:
            clues.append(("left", x, y))
        if pos[x] == pos[y] + 1:
            clues.append(("right_of", x, y))
        if abs(pos[x] - pos[y]) == 1 and x < y:
            clues.append(("next", x, y))
        if abs(pos[x] - pos[y]) > 1 and x < y:
            clues.append(("apart", x, y))
    for x in truth:
        if pos[x] in (0, n - 1):
            clues.append(("end", x))
        else:
            clues.append(("not_end", x))
        for k in range(n):
            if pos[x] != k:
                clues.append(("not_at", x, k))
        clues.append(("at", x, pos[x]))
    return clues


def _order_holds(clue, perm):
    pos = {item: i for i, item in enumerate(perm)}
    kind, n = clue[0], len(perm)
    if kind == "left":
        return pos[clue[1]] < pos[clue[2]]
    if kind == "right_of":
        return pos[clue[1]] == pos[clue[2]] + 1
    if kind == "next":
        return abs(pos[clue[1]] - pos[clue[2]]) == 1
    if kind == "apart":
        return abs(pos[clue[1]] - pos[clue[2]]) > 1
    if kind == "end":
        return pos[clue[1]] in (0, n - 1)
    if kind == "not_end":
        return pos[clue[1]] not in (0, n - 1)
    if kind == "not_at":
        return pos[clue[1]] != clue[2]
    if kind == "at":
        return pos[clue[1]] == clue[2]
    raise ValueError(kind)


def solve_order(items, clues):
    return [p for p in itertools.permutations(items) if all(_order_holds(c, p) for c in clues)]


def _order_text(clue, noun, place):
    kind = clue[0]
    if kind == "left":
        return f"The {clue[1].lower()} {noun} is somewhere to the left of the {clue[2].lower()} {noun}."
    if kind == "right_of":
        return f"The {clue[1].lower()} {noun} is directly to the right of the {clue[2].lower()} {noun}."
    if kind == "next":
        return f"The {clue[1].lower()} and {clue[2].lower()} {noun}s are side by side."
    if kind == "apart":
        return f"The {clue[1].lower()} and {clue[2].lower()} {noun}s are not next to each other."
    if kind == "end":
        return f"The {clue[1].lower()} {noun} is at one of the two ends."
    if kind == "not_end":
        return f"The {clue[1].lower()} {noun} is not at either end."
    if kind == "not_at":
        return f"The {clue[1].lower()} {noun} is not on {place} {clue[2] + 1}."
    return f"The {clue[1].lower()} {noun} is on {place} {clue[2] + 1}."


def gen_ordering(rng):
    noun, place, pool = rng.choice(ORDER_THEMES)
    n = rng.choice([4, 4, 5])
    items = rng.sample(pool, n)
    truth = tuple(rng.sample(items, n))
    candidates = [c for c in _order_clues(truth) if c[0] != "at"]
    rng.shuffle(candidates)
    chosen = []
    remaining = list(itertools.permutations(items))
    for clue in candidates:
        narrowed = [p for p in remaining if _order_holds(clue, p)]
        if len(narrowed) < len(remaining):
            chosen.append(clue)
            remaining = narrowed
        if len(remaining) == 1:
            break
    if len(remaining) != 1:
        return None
    for clue in list(chosen):  # drop clues that aren't needed
        trial = [c for c in chosen if c != clue]
        if len(solve_order(items, trial)) == 1:
            chosen = trial
    if not 3 <= len(chosen) <= 7:
        return None
    answer = " ".join(t.lower() for t in truth)
    item_list = ", ".join(i.lower() for i in sorted(items))
    q = (f"{n} {noun}s sit on {n} {place}s in a row, numbered 1 to {n} from left to right: {item_list}. "
         "The door only opens if you name them in the right order.\n\nYou know that:\n"
         + "\n".join(f"• {_order_text(c, noun, place)}" for c in chosen)
         + f"\n\nWhat is the order? Type the {noun}s from left to right, separated by spaces.")
    first = chosen[0]
    hints = ["Draw the row of positions and try placing the most restricted item first.",
             f"Start from this clue: {_order_text(first, noun, place)}",
             f"From left to right, the first two are the {truth[0].lower()} and {truth[1].lower()} {noun}s."]
    return _record(5, "ordering", _title(rng, "ordering"), q, [answer], hints, answer.upper(),
                   "Order: " + ", ".join(t.lower() for t in truth) + ".",
                   {"items": items, "clues": [list(c) for c in chosen], "noun": noun})


# -- position 6: code breaking -------------------------------------------------------------

def gen_code(rng):
    word = rng.choice(CODE_WORDS)
    kind = rng.choice(["affine", "affine", "affine", "reverse", "binary", "keypad"])
    if kind == "affine":
        a, b = rng.choice([1, 2, 3, 4]), rng.randint(1, 12)
        nums = [a * (ALPHABET.index(c) + 1) + b for c in word]
        how = (f"I added {b} to each letter's number" if a == 1
               else f"I multiplied each letter's number by {a}, then added {b}")
        q = (f"A ledger lies open on the desk. One line of numbers is circled:\n\n<b>{' · '.join(map(str, nums))}</b>\n\n"
             f"In the margin: \"Each number was once a letter (A = 1, B = 2 ... Z = 26). {how}. "
             "Undo what I did - in reverse - to read the word.\"\n\nWhat is the word?")
        undo = f"subtract {b}" + ("" if a == 1 else f", then divide by {a}")
        hints = [f"Undo the steps in reverse order: {undo}.",
                 f"The first number {nums[0]} becomes {ALPHABET.index(word[0]) + 1}, which is {word[0]}.",
                 f"The word starts with {word[:3]}."]
        meta = {"kind": kind, "a": a, "b": b, "nums": nums}
    elif kind == "reverse":
        b = rng.choice([0, 0, 0] + list(range(1, 11)))
        nums = [26 - ALPHABET.index(c) + b for c in word]
        extra = f" Then I added {b} to every number." if b else ""
        q = (f"A scroll hangs on the wall with a row of numbers:\n\n<b>{' · '.join(map(str, nums))}</b>\n\n"
             f"At the bottom it says: \"Here the alphabet runs backwards: Z = 1, Y = 2, X = 3 ... A = 26.{extra}\"\n\n"
             "What is the word?")
        first = f"{nums[0]} - {b} = {nums[0] - b}" if b else str(nums[0])
        hints = [("First subtract " + str(b) + " from every number, then t" if b else "T")
                 + "urn each number into a letter, counting from Z backwards.",
                 f"{first}; counted back from Z that is {word[0]}.", f"The word starts with {word[:3]}."]
        meta = {"kind": kind, "nums": nums, "b": b}
    elif kind == "binary":
        rows = ["".join("●" if bit == "1" else "○" for bit in format(ALPHABET.index(c) + 1, "05b")) for c in word]
        q = (f"A row of five lamps blinks out {len(word)} patterns (● = on, ○ = off):\n\n<code>" + "\n".join(rows)
             + "</code>\n\nA plaque says: \"The lamps are worth 16, 8, 4, 2 and 1 from left to right. "
             "Add up the lit lamps to find each letter's place in the alphabet (A = 1).\"\n\nWhat is the word?")
        hints = ["Each line is one letter. Add the values of the lamps that are on.",
                 f"The first line {rows[0]} adds up to {ALPHABET.index(word[0]) + 1}, which is {word[0]}.",
                 f"The word starts with {word[:3]}."]
        meta = {"kind": kind, "rows": rows}
    else:
        groups = [LETTER_KEY[c][0] * LETTER_KEY[c][1] for c in word]
        q = ("An old mobile phone lies on the desk. Its last message, typed the old-fashioned way, is still on the screen:\n\n"
             f"<b>{' · '.join(groups)}</b>\n\n"
             "A sticky note: \"Remember pressing a key several times to get one letter? 2 = A, 22 = B, 222 = C.\"\n\nWhat is the message?")
        hints = ["Each group of digits is one letter on a phone keypad.",
                 "Key 2 = ABC, 3 = DEF, 4 = GHI, 5 = JKL, 6 = MNO, 7 = PQRS, 8 = TUV, 9 = WXYZ.",
                 f"The message starts with {word[:3]}."]
        meta = {"kind": kind, "groups": groups}
    return _record(6, "code", _title(rng, f"code:{kind}"), q, [word.lower()], hints, word,
                   f"Decoded: {word}.", meta)


# -- position 7: final locks -------------------------------------------------------------

CODES = [d for d in itertools.permutations(range(10), 4) if d[0] != 0]
_MASK_CACHE: dict[tuple, int] = {}


def _keypad_holds(clue, d):
    kind = clue[0]
    if kind == "sum":
        return sum(d) == clue[1]
    if kind == "rel_sum":
        return d[clue[1]] == d[clue[2]] + d[clue[3]]
    if kind == "times":
        return d[clue[1]] == clue[3] * d[clue[2]]
    if kind == "more":
        return d[clue[1]] - d[clue[2]] == clue[3]
    if kind == "greater":
        return d[clue[1]] > d[clue[2]]
    if kind == "parity":
        return d[clue[1]] % 2 == clue[2]
    if kind == "has":
        return clue[1] in d
    if kind == "lacks":
        return clue[1] not in d
    if kind == "odd_count":
        return sum(x % 2 for x in d) == clue[1]
    if kind == "is":
        return d[clue[1]] == clue[2]
    raise ValueError(kind)


def _mask(clue) -> int:
    mask = _MASK_CACHE.get(clue)
    if mask is None:
        mask = 0
        for i, d in enumerate(CODES):
            if _keypad_holds(clue, d):
                mask |= 1 << i
        _MASK_CACHE[clue] = mask
    return mask


def solve_keypad(clues):
    mask = (1 << len(CODES)) - 1
    for clue in clues:
        mask &= _mask(tuple(clue))
    return [CODES[i] for i in range(len(CODES)) if mask >> i & 1]


def _keypad_text(clue):
    kind, o = clue[0], ORDINALS
    if kind == "sum":
        return f"All four digits add up to {clue[1]}."
    if kind == "rel_sum":
        return f"The {o[clue[1]]} digit is the {o[clue[2]]} digit plus the {o[clue[3]]} digit."
    if kind == "times":
        return f"The {o[clue[1]]} digit is {'twice' if clue[3] == 2 else 'three times'} the {o[clue[2]]} digit."
    if kind == "more":
        return f"The {o[clue[1]]} digit is {clue[3]} more than the {o[clue[2]]} digit."
    if kind == "greater":
        return f"The {o[clue[1]]} digit is bigger than the {o[clue[2]]} digit."
    if kind == "parity":
        return f"The {o[clue[1]]} digit is {'odd' if clue[2] else 'even'}."
    if kind == "has":
        return f"The digit {clue[1]} appears somewhere in the code."
    if kind == "lacks":
        return f"The digit {clue[1]} does not appear in the code."
    if kind == "odd_count":
        return f"Exactly {clue[1]} of the digits {'is' if clue[1] == 1 else 'are'} odd."
    if clue[2] == 7:
        return f"The {o[clue[1]]} digit is the number of the room you are standing in."
    return f"The {o[clue[1]]} digit is {clue[2]}."


def gen_keypad(rng):
    d = rng.choice(CODES)
    cands = [("sum", sum(d)), ("odd_count", sum(x % 2 for x in d))]
    for i, j in itertools.permutations(range(4), 2):
        if d[i] > d[j]:
            cands.append(("greater", i, j))
            cands.append(("more", i, j, d[i] - d[j]))
        for m in (2, 3):
            if d[j] and d[i] == m * d[j]:
                cands.append(("times", i, j, m))
    for i, j, k in itertools.permutations(range(4), 3):
        if j < k and d[i] == d[j] + d[k]:
            cands.append(("rel_sum", i, j, k))
    for i in range(4):
        cands.append(("parity", i, d[i] % 2))
    for x in range(10):
        cands.append(("has", x) if x in d else ("lacks", x))
    if 7 in d:
        cands.append(("is", d.index(7), 7))
    rng.shuffle(cands)
    # Prefer relational clues: they make for a satisfying deduction.
    cands.sort(key=lambda c: {"rel_sum": 0, "times": 0, "more": 1, "sum": 1, "is": 2}.get(c[0], 3) + rng.random())
    chosen, mask = [], (1 << len(CODES)) - 1
    for clue in cands:
        narrowed = mask & _mask(clue)
        if narrowed != mask:
            chosen.append(clue)
            mask = narrowed
        if mask.bit_count() == 1:
            break
    if mask.bit_count() != 1:
        return None
    for clue in list(chosen):
        trial = [c for c in chosen if c != clue]
        if len(solve_keypad(trial)) == 1:
            chosen = trial
    if not 4 <= len(chosen) <= 7 or sum(c[0] == "is" for c in chosen) > 1:
        return None
    code = "".join(map(str, d))
    q = ("The final door has a 4-digit keypad. Clues are scratched into the walls:\n\n"
         "🔢 \"All four digits are different, and the first digit is not zero.\"\n"
         + "\n".join(f"🔎 \"{_keypad_text(c)}\"" for c in chosen)
         + "\n\nEnter the code to escape!")
    hints = ["Start with the clue that narrows things down the most, then test the others.",
             f"The first digit is {d[0]}.", f"The code starts with {code[:2]}."]
    return _record(7, "keypad", _title(rng, "keypad"), q, [code], hints, code,
                   "Only " + code + " satisfies every clue.", {"clues": [list(c) for c in chosen]})


def _letter_clue(rng, letter):
    n = ALPHABET.index(letter)
    options = [f"The {n + 1}{'st' if n + 1 in (1, 21) else 'nd' if n + 1 in (2, 22) else 'rd' if n + 1 in (3, 23) else 'th'} letter of the alphabet."]
    if n > 0:
        options.append(f"The letter that comes just after {ALPHABET[n - 1]}.")
    if n < 25:
        options.append(f"The letter that comes just before {ALPHABET[n + 1]}.")
    if n >= 3:
        k = rng.randint(2, min(5, n))
        options.append(f"The letter {k} places after {ALPHABET[n - k]}.")
    if n <= 22:
        k = rng.randint(2, min(5, 25 - n))
        options.append(f"The letter {k} places before {ALPHABET[n + k]}.")
    back = 26 - n
    options.append(f"The {back}{'st' if back in (1, 21) else 'nd' if back in (2, 22) else 'rd' if back in (3, 23) else 'th'}"
                   " letter of the alphabet, counting backwards from Z.")
    # Plain "Nth letter" and "just after/before" clues are easy; favour the harder kinds.
    return rng.choice(options[-2:] + options) if len(options) > 3 else rng.choice(options)


def gen_scatter(rng):
    word = rng.choice([w for w in WORDS if 5 <= len(w) <= 7])
    tagged = [(ROMAN[i], _letter_clue(rng, c)) for i, c in enumerate(word)]
    rng.shuffle(tagged)
    q = (f"The exit word has been broken into {len(word)} letters and scattered around the room. "
         "Each clue is tagged with a Roman numeral telling you where its letter goes:\n\n"
         + "\n".join(f"({tag}) {text}" for tag, text in tagged)
         + "\n\nWhat is the exit word?")
    hints = ["Solve each clue to get one letter, then put them in Roman-numeral order: I, II, III ...",
             f"Letter I is {word[0]} and letter II is {word[1]}.", f"The word starts with {word[:3]}."]
    return _record(7, "scatter", _title(rng, "scatter"), q, [word.lower()], hints, word,
                   f"The letters spell {word}.", {"tagged": tagged})


GENERATORS = {
    1: [gen_fact_lock, gen_arith_lock],
    2: [gen_sequence],
    3: [gen_cipher],
    4: [gen_acrostic],
    5: [gen_ordering],
    6: [gen_code],
    7: [gen_keypad, gen_keypad, gen_scatter],
}


# -- verification -----------------------------------------------------------------

def _letter_from_clue(text):
    words = text.rstrip(".").split()
    if text.startswith("The letter that comes just after"):
        return ALPHABET[ALPHABET.index(words[-1]) + 1]
    if text.startswith("The letter that comes just before"):
        return ALPHABET[ALPHABET.index(words[-1]) - 1]
    if " places after " in text:
        return ALPHABET[ALPHABET.index(words[-1]) + int(words[2])]
    if " places before " in text:
        return ALPHABET[ALPHABET.index(words[-1]) - int(words[2])]
    number = int("".join(ch for ch in words[1] if ch.isdigit()))
    if "counting backwards from Z" in text:
        return ALPHABET[26 - number]
    return ALPHABET[number - 1]


def solve_record(rec) -> str:
    """Independently work out the answer from the puzzle's parameters."""
    fam, meta = rec["family"], rec["meta"]
    if fam == "fact_lock":
        values = dict((p, v) for v, p in FACTS)
        return "".join(str(values[p]) for p in meta["facts"])
    if fam == "arith_lock":
        digits = [meta["first"]]
        for op in meta["steps"]:
            prev = digits[-1]
            if op == "double":
                digits.append(prev * 2)
            elif op == "half":
                digits.append(prev // 2)
            else:
                kind, k = op.split(":")
                digits.append(prev + int(k) if kind == "plus" else prev - int(k))
        assert all(0 <= x <= 9 for x in digits)
        return "".join(map(str, digits))
    if fam == "sequence":
        t, kind = meta["terms"], meta["kind"]
        if kind in ("arith", "desc"):
            assert len({b - a for a, b in zip(t, t[1:])}) == 1
            return str(t[-1] + (t[1] - t[0]))
        if kind == "geom":
            assert len({b // a for a, b in zip(t, t[1:])}) == 1
            return str(t[-1] * (t[1] // t[0]))
        if kind == "alt":
            return str(t[-1] + (t[1] - t[0]))
        if kind == "square":
            diffs = [b - a for a, b in zip(t, t[1:])]
            assert len({b - a for a, b in zip(diffs, diffs[1:])}) == 1
            return str(t[-1] + diffs[-1] + 2)
        return str(t[-1] + t[-2])
    if fam == "cipher":
        coded = meta["coded"]
        return (_atbash(coded) if meta["kind"] == "atbash" else _shift(coded, -meta["shift"])).lower()
    if fam == "acrostic":
        if meta["kind"] == "numbered":
            return "".join(name[0] for _, name in sorted(meta["items"])).lower()
        idx = 0 if meta["kind"] == "first" else -1
        return "".join(name[idx] for name in meta["items"]).lower()
    if fam == "ordering":
        solutions = solve_order(meta["items"], [tuple(c) for c in meta["clues"]])
        assert len(solutions) == 1, "ordering puzzle must have exactly one solution"
        return " ".join(solutions[0]).lower()
    if fam == "code":
        kind = meta["kind"]
        if kind == "affine":
            letters = []
            for n in meta["nums"]:
                assert (n - meta["b"]) % meta["a"] == 0
                letters.append(ALPHABET[(n - meta["b"]) // meta["a"] - 1])
            return "".join(letters).lower()
        if kind == "reverse":
            return "".join(ALPHABET[26 - (n - meta.get("b", 0))] for n in meta["nums"]).lower()
        if kind == "binary":
            return "".join(ALPHABET[int(r.replace("●", "1").replace("○", "0"), 2) - 1] for r in meta["rows"]).lower()
        return "".join(KEYPAD[g[0]][len(g) - 1] for g in meta["groups"]).lower()
    if fam == "keypad":
        solutions = solve_keypad([tuple(c) for c in meta["clues"]])
        assert len(solutions) == 1, "keypad must have exactly one solution"
        return "".join(map(str, solutions[0]))
    if fam == "scatter":
        letters = {tag: _letter_from_clue(text) for tag, text in meta["tagged"]}
        return "".join(letters[r] for r in ROMAN[:len(letters)]).lower()
    raise ValueError(fam)


def verify_record(rec) -> None:
    """Raise AssertionError unless the record is complete and its answer is right."""
    assert rec["title"] and rec["question"] and len(rec["hints"]) == 3 and rec["accepted_answers"]
    assert is_correct(rec["solution"], rec["accepted_answers"]), rec["id"]
    assert is_correct(solve_record(rec), rec["accepted_answers"]), rec["id"]


# -- bank assembly ---------------------------------------------------------------------

def core_key(rec) -> str:
    """What makes a puzzle different: its content, not its scenery text.

    Two puzzles with the same numbers, clues or coded message count as the
    same puzzle even if the room description or title differs.
    """
    meta = {k: v for k, v in rec["meta"].items() if k != "clue"}
    return rec["family"] + ":" + json.dumps(meta, sort_keys=True)


def generate_position(position: int, count: int, seed: str = BANK_SEED) -> list[dict]:
    rng = random.Random(f"{seed}:position:{position}")
    seen, out = set(), []
    gens = GENERATORS[position]
    attempts = 0
    while len(out) < count:
        attempts += 1
        if attempts > count * 50:
            raise RuntimeError(f"Could not generate {count} unique puzzles for position {position}")
        rec = rng.choice(gens)(rng)
        if rec is None:
            continue
        key = core_key(rec)
        if key in seen:
            continue
        verify_record({**rec, "id": "check"})
        seen.add(key)
        out.append(rec)
    return out


def generate_bank(set_count: int = SET_COUNT, seed: str = BANK_SEED) -> list[dict]:
    columns = {p: generate_position(p, set_count, seed) for p in range(1, 8)}
    width = len(str(set_count))
    answer = lambda s, p: canonical(columns[p][s]["solution"])

    def set_answers(s, skip):
        return {answer(s, q) for q in range(1, 8) if q != skip}

    # Keep answers distinct within each set, swapping puzzles between sets in
    # the same column: first with a later set, else with an earlier one whose
    # own answers stay distinct after the swap.
    for s in range(set_count):
        for p in range(1, 8):
            if answer(s, p) not in {answer(s, q) for q in range(1, p)}:
                continue
            mine = {answer(s, q) for q in range(1, p)}
            later = [t for t in range(s + 1, set_count) if answer(t, p) not in mine]
            earlier = [t for t in range(s) if answer(t, p) not in set_answers(s, p)
                       and answer(s, p) not in set_answers(t, p)]
            if not later and not earlier:
                raise RuntimeError(f"Could not keep answers distinct in set {s + 1}")
            t = later[0] if later else earlier[0]
            columns[p][s], columns[p][t] = columns[p][t], columns[p][s]

    bank = []
    for s in range(set_count):
        set_id = f"S{s + 1:0{width}d}"
        for p in range(1, 8):
            bank.append({"id": f"{set_id}-R{p}", "set": set_id, **columns[p][s]})
    return bank


def write_bank(bank: list[dict], path: Path = BANK_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the file byte-for-byte reproducible.
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
        for rec in bank:
            gz.write((json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))


def read_bank(path: Path = BANK_PATH):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the puzzle bank")
    parser.add_argument("--sets", type=int, default=SET_COUNT)
    parser.add_argument("--out", type=Path, default=BANK_PATH)
    args = parser.parse_args()
    bank = generate_bank(args.sets)
    write_bank(bank, args.out)
    print(f"Wrote {len(bank)} puzzles in {args.sets} sets to {args.out}")
