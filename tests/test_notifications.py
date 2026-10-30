import pytest
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TimedOut

from tests.conftest import ANSWERS, GROUP_A, GROUP_B


async def test_flush_delivers_room1_to_both_sides(world, notifier, fake_bot):
    world.setup_1v1()
    sent = await notifier.flush()
    assert sent == len(world.outbox())
    for chat in (1, 2):
        assert any("ROOM 1 / 7" in t for t in fake_bot.texts(chat))
    assert await notifier.flush() == 0  # nothing re-sent


async def test_order_is_preserved_per_chat(world, notifier, fake_bot):
    world.setup_1v1()
    world.solve(1, 1, "private", [1, 2])
    await notifier.flush()
    texts = [t for t in fake_bot.texts(1) if "/ 7</b>" in t]
    assert ["ROOM 1 / 7" in texts[0], "ROOM 2 / 7" in texts[1], "ROOM 3 / 7" in texts[2]] == [True] * 3


async def test_transient_failure_is_retried_later(world, notifier, fake_bot):
    world.setup_1v1()
    fake_bot.fail[1] = [NetworkError("boom")]
    await notifier.flush()
    assert fake_bot.texts(1) == []                     # chat 1 paused to keep order
    assert any("ROOM 1" in t for t in fake_bot.texts(2))  # other chat unaffected
    pending = world.db.query_all("SELECT * FROM outbox WHERE status = 'pending'")
    assert pending and all(r["chat_id"] == 1 for r in pending)
    world.clock.advance(5)
    await notifier.flush()
    assert any("ROOM 1 / 7" in t for t in fake_bot.texts(1))
    assert world.db.query_one("SELECT COUNT(*) AS n FROM outbox WHERE status != 'sent'")["n"] == 0


async def test_retry_after_waits_and_resends(world, notifier, fake_bot):
    world.setup_1v1()
    fake_bot.fail[2] = [RetryAfter(1)]
    await notifier.flush()
    assert any("ROOM 1" in t for t in fake_bot.texts(2))


async def test_permanent_failure_marks_failed_without_blocking(world, notifier, fake_bot):
    world.setup_1v1()
    fake_bot.fail[2] = [Forbidden("bot was blocked by the user")] * 10
    await notifier.flush()
    failed = world.db.query_all("SELECT * FROM outbox WHERE status = 'failed'")
    assert failed and all(r["chat_id"] == 2 for r in failed)
    assert "forbidden" in failed[0]["last_error"]
    assert any("ROOM 1" in t for t in fake_bot.texts(1))


async def test_gives_up_after_max_attempts(world, notifier, fake_bot):
    world.setup_1v1()
    fake_bot.fail[1] = [TimedOut()] * 20
    for _ in range(5):
        world.clock.advance(1000)
        await notifier.flush()
    assert world.db.query_one(
        "SELECT status FROM outbox WHERE chat_id = 1 ORDER BY message_id LIMIT 1")["status"] == "failed"


async def test_bad_html_falls_back_to_plain_text(world, notifier, fake_bot):
    world.setup_1v1()
    fake_bot.fail[1] = [BadRequest("Can't parse entities: unsupported start tag")]
    await notifier.flush()
    plain = [m for m in fake_bot.sent if m["chat_id"] == 1][0]
    assert plain["parse_mode"] is None and "<b>" not in plain["text"]


async def test_chat_migration_updates_team(world, notifier, fake_bot):
    match_id = world.setup_2v2()
    fake_bot.fail[GROUP_A] = [ChatMigrated(-100999)]
    await notifier.flush()
    assert world.team(match_id, "A")["chat_id"] == -100999
    assert any("ROOM 1 / 7" in t for t in fake_bot.texts(-100999))
    # gameplay continues in the migrated group
    assert world.answer(1, -100999, "supergroup", ANSWERS[1]).text is None


async def test_restart_does_not_resend_delivered_puzzles(world, notifier, fake_bot):
    world.setup_2v2()
    await notifier.flush()
    before = len(fake_bot.sent)
    world.restart()
    world.engine.recover()
    from notifications import Notifier
    from tests.conftest import ASSETS_DIR
    again = Notifier(world.db, fake_bot, ASSETS_DIR, clock=world.clock)
    assert await again.flush() == 0
    assert len(fake_bot.sent) == before


async def test_restart_delivers_messages_that_were_pending(world, notifier, fake_bot):
    world.setup_1v1()
    world.solve(1, 1, "private", [1])     # queued but the "process dies" before flushing
    world.restart()
    world.engine.recover()
    from notifications import Notifier
    from tests.conftest import ASSETS_DIR
    await Notifier(world.db, fake_bot, ASSETS_DIR, clock=world.clock).flush()
    assert sum("ROOM 2 / 7" in t for t in fake_bot.texts(1)) == 1


async def test_image_puzzles_send_photo(world, fake_bot, tmp_path):
    from notifications import Notifier, enqueue_photo
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n")
    with world.db.transaction() as conn:
        enqueue_photo(conn, chat_id=5, image="pic.png", caption="A clue", dedupe_key="x", now=world.clock())
        enqueue_photo(conn, chat_id=5, image="../escape.png", caption=None, dedupe_key="y", now=world.clock())
    await Notifier(world.db, fake_bot, tmp_path, clock=world.clock).flush()
    photos = [m for m in fake_bot.sent if "photo" in m]
    assert len(photos) == 1 and photos[0]["caption"] == "A clue"
    assert world.db.query_one("SELECT status FROM outbox WHERE dedupe_key='y'")["status"] == "failed"


@pytest.mark.parametrize("group", [GROUP_A, GROUP_B])
async def test_progress_alerts_delivered_to_opposing_group(world, notifier, fake_bot, group):
    world.setup_2v2()
    solver_chat = GROUP_A if group == GROUP_B else GROUP_A
    world.solve(1, GROUP_A, "supergroup", [1])
    await notifier.flush()
    alerts = [t for t in fake_bot.texts(GROUP_B) if "opposing team just reached" in t]
    assert len(alerts) == 1 and "Room 2" in alerts[0]
    assert not any("opposing team just reached" in t for t in fake_bot.texts(solver_chat))
