"""Telegram entry point: handlers, keyboards, startup recovery.

Run ``python bot.py`` (polling, the default) or set ``WEBHOOK_URL`` to run
a webhook server. ``python bot.py --init-db`` only creates/migrates the
database and validates puzzles.json, without contacting Telegram.

This module only translates between Telegram and the game services; all
rules live in matchmaking.py, game_engine.py and hint_manager.py.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from dataclasses import dataclass

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ChatMemberUpdated,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import messages as msg
from config import ConfigError, Settings, load_settings
from database import Database
from game_engine import GameEngine, Outcome
from hint_manager import HintManager
from matchmaking import Matchmaking, normalize_code
from notifications import Notifier
from puzzle_manager import PuzzleError, PuzzleProvider
from state import Button, GameError, PuzzleCache

log = logging.getLogger("escape_room")

# Plain text in private chats (never commands) - used for 1v1 answers.
PRIVATE_TEXT = filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE


@dataclass
class Services:
    settings: Settings
    db: Database
    matchmaking: Matchmaking
    engine: GameEngine
    hints: HintManager
    notifier: Notifier | None = None

    @classmethod
    def build(cls, settings: Settings, clock=time.time) -> "Services":
        db = Database(settings.database_path)
        db.migrate()
        provider = PuzzleProvider(settings.puzzles_path, settings.assets_dir)
        cache = PuzzleCache()
        return cls(
            settings=settings,
            db=db,
            matchmaking=Matchmaking(db, provider.get, settings, clock, cache),
            engine=GameEngine(db, settings, clock, cache),
            hints=HintManager(db, settings, clock, cache),
        )


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["services"]


def keyboard(buttons: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(b.label, url=b.url) if b.url
                else InlineKeyboardButton(b.label, callback_data=b.callback)
                for b in row
            ]
            for row in buttons
        ]
    )


MAIN_MENU = [
    [Button("🎮 Create Match", callback="menu:create"), Button("🔑 Join Match", callback="menu:join")],
    [Button("📖 How to Play", callback="menu:howto"), Button("❓ Help", callback="menu:help")],
]
MODE_MENU = [
    [Button("👤 1v1 Individual", callback="create:1v1"), Button("👥 2v2 Team", callback="create:2v2")],
    [Button("⬅️ Back", callback="menu:home")],
]
BACK = [[Button("⬅️ Back to menu", callback="menu:home")]]


async def reply(update: Update, text: str | None, buttons: list[list[Button]] | None = None,
                quote: bool = False) -> None:
    if not text:
        return
    message = update.effective_message
    await message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=keyboard(buttons),
        do_quote=quote, disable_web_page_preview=True,
    )


async def respond(update: Update, context: ContextTypes.DEFAULT_TYPE, outcome: Outcome,
                  quote: bool = False) -> None:
    await reply(update, outcome.text, outcome.buttons, quote=quote)
    await flush(context)


async def flush(context: ContextTypes.DEFAULT_TYPE) -> None:
    notifier = services(context).notifier
    if notifier is not None:
        await notifier.flush()


def command_argument(update: Update) -> str:
    """Everything after the command, preserving the user's own spacing."""
    text = update.effective_message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


# -- pre-processing ---------------------------------------------------------

async def preprocess(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop duplicate updates and keep the user directory current."""
    svc = services(context)
    if not svc.db.mark_update_processed(update.update_id, time.time()):
        log.info("Ignoring duplicate update %s", update.update_id)
        raise ApplicationHandlerStop
    user, chat = update.effective_user, update.effective_chat
    if user is not None and not user.is_bot:
        svc.matchmaking.register_user(
            user.id, user.first_name, user.last_name, user.username,
            chat.id if chat else None, chat.type if chat else None,
        )


# -- commands -----------------------------------------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payload = context.args[0] if context.args else ""
    if normalize_code(payload):
        await do_join(update, context, payload)
        return
    if update.effective_chat.type == ChatType.PRIVATE:
        await reply(update, msg.WELCOME, MAIN_MENU)
    else:
        await reply(update, msg.GROUP_INTRO)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, msg.HELP)


async def create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await reply(update, "🎮 Please create matches in a private chat with me.",
                    [[Button("▶️ Open private chat", url=f"https://t.me/{context.bot.username}")]])
        return
    await reply(update, "Choose a game mode:", MODE_MENU)


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    code = command_argument(update)
    if not code:
        if update.effective_chat.type == ChatType.PRIVATE:
            await update.effective_message.reply_text(msg.JOIN_PROMPT, reply_markup=ForceReply(selective=True))
        else:
            await reply(update, "Usage: <code>/join CODE</code> - use your team's code.")
        return
    await do_join(update, context, code)


async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    chat, user = update.effective_chat, update.effective_user
    outcome = services(context).matchmaking.join(
        user.id, code, chat.id, chat.type, chat.title, context.bot.username
    )
    await respond(update, context, outcome, quote=chat.type != ChatType.PRIVATE)


async def answer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    outcome = services(context).engine.submit_answer(user.id, chat.id, chat.type, command_argument(update))
    await respond(update, context, outcome, quote=chat.type != ChatType.PRIVATE)


async def hint_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    outcome = services(context).hints.request_hint(user.id, chat.id, chat.type)
    await respond(update, context, outcome, quote=chat.type != ChatType.PRIVATE)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    await respond(update, context, services(context).engine.status(user.id, chat.id, chat.type))


async def quit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    await respond(update, context, services(context).engine.request_quit(user.id, chat.id, chat.type))


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "🤔 I don't know that command. Try /help.")


async def private_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    replied = message.reply_to_message
    if replied and replied.from_user and replied.from_user.id == context.bot.id \
            and replied.text == msg.JOIN_PROMPT:
        await do_join(update, context, message.text)
        return
    outcome = services(context).engine.handle_private_text(user.id, update.effective_chat.id, message.text)
    await respond(update, context, outcome)


# -- inline keyboards -------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    svc = services(context)
    user = query.from_user

    async def edit(text: str, buttons: list[list[Button]] | None = None) -> None:
        with contextlib.suppress(Exception):  # e.g. "message is not modified"
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard(buttons),
                                          disable_web_page_preview=True)

    if data.startswith("quit:"):
        parts = data.split(":")
        owner = parts[-1]
        if owner != str(user.id):
            await query.answer("This button belongs to someone else.", show_alert=True)
            return
        await query.answer()
        if parts[1] == "yes" and len(parts) == 4:
            outcome = svc.engine.confirm_quit(user.id, parts[2])
            await edit(outcome.text or "Done.")
            await flush(context)
        else:
            await edit("👍 Carry on - good luck!")
        return

    await query.answer()
    if data == "menu:home":
        await edit(msg.WELCOME, MAIN_MENU)
    elif data == "menu:create":
        await edit("Choose a game mode:", MODE_MENU)
    elif data == "menu:join":
        await context.bot.send_message(query.message.chat.id, msg.JOIN_PROMPT,
                                       reply_markup=ForceReply(selective=True))
    elif data == "menu:howto":
        await edit(msg.HOW_TO_PLAY, BACK)
    elif data == "menu:help":
        await edit(msg.HELP, BACK)
    elif data.startswith("create:"):
        if query.message.chat.type != ChatType.PRIVATE:
            return
        try:
            created = svc.matchmaking.create_match(user.id, data.split(":", 1)[1])
        except GameError as exc:
            await edit(exc.message, BACK)
            return
        text, buttons = created_text(created, context.bot.username)
        await edit(text, buttons)


def created_text(created, bot_username: str) -> tuple[str, list[list[Button]]]:
    if created.mode == "1v1":
        link = f"https://t.me/{bot_username}?start={created.code_b}"
        text = (
            f"✅ <b>1v1 match created!</b>  ID: <code>{created.match_id}</code>\n\n"
            f"Send this invite link to your opponent:\n{link}\n\n"
            f"Or they can open @{bot_username}, press Start and send:\n"
            f"<code>/join {created.code_b}</code>\n\n"
            "The match starts automatically as soon as they join. ⏳"
        )
        share = f"https://t.me/share/url?url={link}&text=Race%20me%20through%20the%20escape%20room!"
        return text, [[Button("📤 Share invite", url=share)]]

    link_a = f"https://t.me/{bot_username}?startgroup={created.code_a}"
    link_b = f"https://t.me/{bot_username}?startgroup={created.code_b}"
    text = (
        f"✅ <b>2v2 match created!</b>  ID: <code>{created.match_id}</code>\n\n"
        f"🅰️ <b>Team A</b> code (you + your teammate): <code>{created.code_a}</code>\n"
        f"🅱️ <b>Team B</b> code (send to the other team): <code>{created.code_b}</code>\n\n"
        "<b>Each team must:</b>\n"
        "1. Create its <b>own</b> Telegram group with both teammates.\n"
        f"2. Add @{bot_username} to that group (the button/link below does this).\n"
        "3. Make sure both teammates have pressed Start in a private chat with me.\n"
        "4. In the group, <b>each</b> teammate sends <code>/join CODE</code> with their team's code.\n\n"
        f"Team A group link: {link_a}\nTeam B group link: {link_b}\n\n"
        "⚠️ Keep your codes private - anyone with a code can take a free seat on that team.\n"
        "The match starts automatically when both teams are complete."
    )
    return text, [[Button("➕ Add me to Team A's group", url=link_a)]]


# -- group lifecycle ------------------------------------------------------------

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change: ChatMemberUpdated = update.my_chat_member
    if change.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    was = change.old_chat_member.status
    now = change.new_chat_member.status
    joined = now in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR) and was in (
        ChatMemberStatus.LEFT, ChatMemberStatus.BANNED
    )
    if joined:
        with contextlib.suppress(Exception):
            await context.bot.send_message(change.chat.id, msg.GROUP_INTRO, parse_mode=ParseMode.HTML)
    elif now in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        log.warning("Bot was removed from chat %s", change.chat.id)


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message.migrate_to_chat_id:
        services(context).engine.migrate_chat(message.chat.id, message.migrate_to_chat_id)
    elif message.migrate_from_chat_id:
        services(context).engine.migrate_chat(message.migrate_from_chat_id, message.chat.id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        with contextlib.suppress(Exception):
            await update.effective_message.reply_text(
                "⚠️ Something went wrong on my side. Your progress is safe - please try again."
            )


# -- lifecycle ------------------------------------------------------------------

async def background_loop(app: Application) -> None:
    svc: Services = app.bot_data["services"]
    while True:
        await asyncio.sleep(svc.settings.outbox_poll_seconds)
        try:
            svc.matchmaking.expire_lobbies()
            svc.db.prune(time.time())
            await svc.notifier.flush()
        except Exception:  # keep the loop alive
            log.exception("Background maintenance failed")


async def on_startup(app: Application) -> None:
    svc: Services = app.bot_data["services"]
    svc.notifier = Notifier(svc.db, app.bot, svc.settings.assets_dir,
                            max_attempts=svc.settings.outbox_max_attempts)
    svc.matchmaking.expire_lobbies()
    svc.engine.recover()
    await svc.notifier.flush()
    private = [
        BotCommand("start", "Main menu"), BotCommand("create", "Create a match"),
        BotCommand("join", "Join with a code"), BotCommand("answer", "Submit an answer"),
        BotCommand("hint", "Use a paid hint"), BotCommand("status", "Match progress"),
        BotCommand("quit", "Leave / forfeit"), BotCommand("help", "Instructions"),
    ]
    group = [c for c in private if c.command not in ("create",)]
    with contextlib.suppress(Exception):
        await app.bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())
    app.bot_data["background"] = asyncio.create_task(background_loop(app))
    log.info("Bot @%s is up", app.bot.username)


async def on_shutdown(app: Application) -> None:
    task = app.bot_data.get("background")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    app.bot_data["services"].db.close()


def build_application(svc: Services, request=None) -> Application:
    builder = (
        ApplicationBuilder()
        .token(svc.settings.bot_token)
        .concurrent_updates(True)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
    )
    if request is not None:  # tests inject a fake Telegram HTTP layer
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()
    app.bot_data["services"] = svc
    app.add_handler(TypeHandler(Update, preprocess), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("create", create_cmd))
    app.add_handler(CommandHandler("join", join_cmd))
    app.add_handler(CommandHandler("answer", answer_cmd))
    app.add_handler(CommandHandler("hint", hint_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("quit", quit_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(MessageHandler(PRIVATE_TEXT, private_text))
    app.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, unknown_cmd))
    app.add_error_handler(on_error)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Competitive Escape Room Telegram bot")
    parser.add_argument("--init-db", action="store_true",
                        help="create/upgrade the database, validate puzzles.json and exit")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(require_token=not args.init_db)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        svc = Services.build(settings)
    except PuzzleError as exc:
        print(f"Puzzle file error: {exc}", file=sys.stderr)
        return 2

    if args.init_db:
        print(f"Database ready at {settings.database_path} (schema v{svc.db.schema_version()}); "
              f"puzzles OK ({settings.puzzles_path}).")
        svc.db.close()
        return 0

    app = build_application(svc)
    if settings.webhook_url:
        app.run_webhook(
            listen=settings.webhook_listen,
            port=settings.webhook_port,
            url_path="telegram",
            webhook_url=settings.webhook_url.rstrip("/") + "/telegram",
            secret_token=settings.webhook_secret or None,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # drop_pending_updates=False: messages sent while the bot was down are
        # processed after a restart instead of being silently discarded.
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
