"""Telegram-layer checks that don't need network access."""

from datetime import datetime, timezone

from telegram import Chat, Message, MessageEntity, Update, User

import bot
from config import Settings


def _update(text, chat_type="private", entities=None):
    chat = Chat(id=1 if chat_type == "private" else -100, type=chat_type)
    user = User(id=1, first_name="A", is_bot=False)
    message = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user,
                      text=text, entities=entities or [])
    return Update(update_id=1, message=message)


def test_commands_never_become_answers():
    cmd = _update("/hint", entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=5)])
    assert not bot.PRIVATE_TEXT.check_update(cmd)
    status = _update("/status@EscapeBot",
                     entities=[MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=17)])
    assert not bot.PRIVATE_TEXT.check_update(status)
    assert bot.PRIVATE_TEXT.check_update(_update("hidden door"))


def test_group_text_is_never_an_answer():
    assert not bot.PRIVATE_TEXT.check_update(_update("369", chat_type="supergroup"))


def test_command_argument_preserves_text():
    assert bot.command_argument(_update("/answer   blue  key ")) == "blue  key "
    assert bot.command_argument(_update("/answer")) == ""


def test_created_text_contains_deep_links():
    from matchmaking import CreatedMatch
    text, buttons = bot.created_text(CreatedMatch("ABC123", "1v1", None, "CODEBBBB"), "EscapeBot")
    assert "https://t.me/EscapeBot?start=CODEBBBB" in text
    text, buttons = bot.created_text(CreatedMatch("ABC123", "2v2", "CODEAAAA", "CODEBBBB"), "EscapeBot")
    assert "startgroup=CODEAAAA" in text and "startgroup=CODEBBBB" in text


def test_application_builds_with_all_handlers(tmp_path):
    settings = Settings(bot_token="123456:TEST", database_path=tmp_path / "b.db")
    svc = bot.Services.build(settings)
    app = bot.build_application(svc)
    commands = {c for h in app.handlers[0] if hasattr(h, "commands") for c in h.commands}
    assert {"start", "create", "join", "answer", "hint", "status", "quit", "help"} <= commands
    svc.db.close()
