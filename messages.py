"""User-facing message templates (Telegram HTML parse mode).

Puzzle text from ``puzzles.json`` is written in Telegram HTML already and is
inserted verbatim; everything that comes from users (names, guesses) is
escaped with :func:`html.escape`.
"""

from __future__ import annotations

import random
from html import escape

from config import ESCAPED_ROOM, PAID_HINTS_PER_SIDE, TOTAL_ROOMS
from puzzle_manager import Puzzle

WRONG_ANSWER_REPLIES = (
    "Not quite! Think again. 🔐",
    "The lock doesn't budge. Try again! 🔒",
    "Nope - that's not it. Keep thinking! 🧩",
)


def wrong_answer() -> str:
    return random.choice(WRONG_ANSWER_REPLIES)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(plural(hours, "hour"))
    if hours or minutes:
        parts.append(plural(minutes, "minute"))
    parts.append(plural(secs, "second"))
    return ", ".join(parts)


def room_label(room: int) -> str:
    return "Escaped 🏁" if room >= ESCAPED_ROOM else f"Room {room} / {TOTAL_ROOMS}"


def commands_footer(mode: str) -> str:
    if mode == "1v1":
        return (
            "<b>Commands:</b> /answer &lt;guess&gt; · /hint · /status\n"
            "<i>Tip: you can also just type your answer.</i>"
        )
    return "<b>Commands:</b> /answer &lt;guess&gt; · /hint · /status"


def room_message(puzzle: Puzzle, mode: str, header: str | None = None) -> str:
    lines = []
    if header:
        lines += [header, ""]
    lines += [
        f"🚪 <b>ROOM {puzzle.room} / {TOTAL_ROOMS}</b>",
        f"🔐 <b>{escape(puzzle.title.upper())}</b>  ·  <i>{escape(puzzle.difficulty)}</i>",
        "",
        "<b>Your puzzle:</b>",
        puzzle.question,
    ]
    if puzzle.image:
        lines += ["", "🖼️ <i>See the image below.</i>"]
    lines += ["", commands_footer(mode)]
    return "\n".join(lines)


def app_guess_echo(name: str, guess: str, correct: bool) -> str:
    verdict = "✅ Correct!" if correct else "❌ Not quite."
    return f"🔎 <b>{escape(name)}</b> tried <code>{escape(guess[:100])}</code> in the room - {verdict}"


def unlock_header(solved_room: int, solver: str | None) -> str:
    by = f" (solved by {escape(solver)})" if solver else ""
    return (
        f"🔓 <b>ROOM {solved_room + 1} UNLOCKED!</b>\n"
        f"You have successfully escaped Room {solved_room}{by}.\n"
        "Your next challenge awaits..."
    )


def opponent_progress(room_reached: int, mode: str) -> str:
    who = "Your opponent" if mode == "1v1" else "The opposing team"
    return f"📣 {who} just reached <b>Room {room_reached}</b>. 🔓"


def paid_hint_message(room: int, hint_number: int, text: str, paid_left: int,
                      requested_by: str | None, opponent_got_free: bool) -> str:
    by = f" (requested by {escape(requested_by)})" if requested_by else ""
    lines = [
        f"💡 <b>Hint {hint_number}/3 for Room {room}</b>{by}",
        text,
        "",
        f"Paid hints left: <b>{paid_left} / {PAID_HINTS_PER_SIDE}</b>",
    ]
    if opponent_got_free:
        lines.append("🎁 Your opponent received a free hint for the room they are in.")
    return "\n".join(lines)


def free_hint_message(room: int, hint_number: int, text: str, mode: str) -> str:
    who = "Your opponent" if mode == "1v1" else "The opposing team"
    return (
        f"🎁 <b>FREE HINT!</b> {who} just used a paid hint, so you get one on the house.\n\n"
        f"💡 <b>Hint {hint_number}/3 for Room {room}</b>\n{text}\n\n"
        "<i>Free hints don't use up your paid hints.</i>"
    )


def opponent_used_hint_no_free(mode: str) -> str:
    who = "Your opponent" if mode == "1v1" else "The opposing team"
    return (
        f"ℹ️ {who} used a paid hint. You've already received every hint for your "
        "current room, so there was no free hint to give you."
    )


def match_started(mode: str, side_name: str, opponent_name: str, match_id: str) -> str:
    mode_label = "1v1 Individual" if mode == "1v1" else "2v2 Team"
    lines = [
        "🚨 <b>THE MATCH HAS BEGUN!</b>",
        "",
        f"Match: <code>{escape(match_id)}</code>  ·  Mode: <b>{mode_label}</b>",
        f"You: <b>{escape(side_name)}</b>",
        f"Opponent: <b>{escape(opponent_name)}</b>",
        "",
        f"Escape all {TOTAL_ROOMS} rooms before your opponent does.",
        f"You have <b>{PAID_HINTS_PER_SIDE} paid hints</b> for the whole game. "
        "Every time you use one, your opponent gets a free hint - use them wisely!",
    ]
    if mode == "2v2":
        lines.append("Either teammate can submit answers with /answer &lt;guess&gt;.")
    lines += ["", "Room 1 is coming up... good luck! 🍀"]
    return "\n".join(lines)


def victory_message(winner_name: str, duration: float, paid_used: int, free_received: int) -> str:
    return (
        "🏆 <b>ESCAPE SUCCESSFUL!</b>\n\n"
        f"<b>{escape(winner_name)}</b> {'have' if ' & ' in winner_name else 'has'} escaped!\n"
        f"Time taken: {format_duration(duration)}.\n"
        f"Paid hints used: {paid_used}.\n"
        f"Free hints received: {free_received}.\n\n"
        f"Congratulations! You have escaped all {TOTAL_ROOMS} rooms."
    )


def defeat_message(winner_name: str, duration: float, your_room: int) -> str:
    return (
        "🏁 <b>MATCH OVER</b>\n\n"
        f"<b>{escape(winner_name)}</b> escaped first, in {format_duration(duration)}.\n"
        f"You reached Room {min(your_room, TOTAL_ROOMS)} of {TOTAL_ROOMS}.\n\n"
        "So close! Use /start to play again. 🔁"
    )


def forfeit_winner_message(quitter_name: str, duration: float) -> str:
    return (
        "🏳️ <b>YOUR OPPONENT FORFEITED</b>\n\n"
        f"{escape(quitter_name)} quit the match, so you win by forfeit! 🏆\n"
        f"Match time: {format_duration(duration)}.\n\n"
        "Use /start to play again."
    )


def forfeit_loser_message(quitter_name: str, team_rule: bool, duration: float) -> str:
    rule = " Because one teammate quit, the whole team forfeits." if team_rule else ""
    return (
        "🏳️ <b>MATCH FORFEITED</b>\n\n"
        f"{escape(quitter_name)} quit the match.{rule}\n"
        f"The win goes to your opponent. Match time: {format_duration(duration)}.\n\n"
        "Use /start to play again."
    )


WELCOME = (
    "🗝️ <b>Welcome to the Competitive Escape Room!</b>\n\n"
    f"Race a rival through {TOTAL_ROOMS} locked rooms. Each room hides one puzzle - "
    "solve it to unlock the next. The first side to escape Room "
    f"{TOTAL_ROOMS} wins.\n\n"
    "Play <b>1v1</b> against a friend, or <b>2v2</b> as a team of two."
)

HOW_TO_PLAY = (
    "📖 <b>HOW TO PLAY</b>\n\n"
    f"• Both sides get the same {TOTAL_ROOMS} puzzles in the same order, but progress independently.\n"
    "• Solve a room to unlock the next one. Your opponent is told which room you reach - "
    "never your puzzle, guesses or hints.\n"
    f"• Each side has <b>{PAID_HINTS_PER_SIDE} paid hints</b> for the whole game. Using one gives "
    "your opponent a <b>free hint</b> for the room they're in.\n"
    "• Wrong answers cost nothing - but guesses are rate-limited, so think first!\n"
    f"• First to escape Room {TOTAL_ROOMS} wins. /quit forfeits the match.\n\n"
    "<b>1v1</b>\n"
    "1. Create a match → 1v1 and send the invite link to your opponent.\n"
    "2. Play in your private chat with me: type your answer or use /answer.\n\n"
    "<b>2v2</b>\n"
    "1. Create a match → 2v2. You'll get one code for Team A and one for Team B.\n"
    "2. Each team makes its <b>own</b> Telegram group with both teammates and me.\n"
    "3. Each teammate opens a private chat with me and presses Start once.\n"
    "4. In the team group, each teammate sends <code>/join CODE</code> with their team's code.\n"
    "5. In the group, use /answer &lt;guess&gt; - normal chat is never treated as an answer, "
    "so discuss freely."
)

HELP = (
    "❓ <b>COMMANDS</b>\n\n"
    "/start - main menu\n"
    "/create - create a new match\n"
    "/join CODE - join a match (1v1: here; 2v2: in your team group)\n"
    "/answer GUESS - submit an answer\n"
    "/hint - use one of your 3 paid hints\n"
    "/status - match progress and hints\n"
    "/quit - leave the lobby, or forfeit an active match\n"
    "/help - this message\n\n"
    "In 1v1 you can also just type your answer in our private chat."
)

GROUP_INTRO = (
    "👋 Hi! I run the Competitive Escape Room.\n\n"
    "To use this group as your <b>2v2 team group</b>, each teammate should:\n"
    "1. Open a private chat with me and press <b>Start</b> once.\n"
    "2. Send <code>/join CODE</code> here, using your team's code.\n\n"
    "Only /commands reach the game here - chat freely with your teammate."
)

JOIN_PROMPT = "🔑 Send me your join code (reply to this message):"
