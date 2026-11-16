"""BOT-1…BOT-3: бот отвечает только владельцу, не блокируется фоновыми задачами, переживает перезапуск."""

from __future__ import annotations

import asyncio
import logging
import time as systime

import pytest
from sqlalchemy import insert, select

from hunter_core.observability.heartbeat import Heartbeat
from hunter_core.scheduler import JobRunner
from hunter_core.storage.tables import outbox
from tests.fakes import FakeClock, FakeSender, fake_bot, make_message_update
from vacancy_hunter.app import Application
from vacancy_hunter.bot import BotContext, build_dispatcher
from vacancy_hunter.config import load_app_config

pytestmark = pytest.mark.integration

OWNER = 111
STRANGER = 999


def context(engine, wakes):
    return BotContext(engine=engine, owner_id=OWNER, timezone="Asia/Tbilisi", report_time="07:00",
                      wake_sender=lambda: wakes.append(1), clock=FakeClock())


async def outbox_rows(engine):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(outbox).order_by(outbox.c.id))).mappings()]


# ------------------------------------------------------------------ BOT-1
async def test_bot1_stranger_gets_no_answer_and_is_logged(aengine, caplog):
    wakes = []
    dp = build_dispatcher(context(aengine, wakes))
    bot = fake_bot()
    with caplog.at_level(logging.WARNING, logger="hunter_core.access"):
        await dp.feed_update(bot, make_message_update(1, STRANGER, "/start"))
        await dp.feed_update(bot, make_message_update(2, STRANGER, "/status"))
    assert await outbox_rows(aengine) == []
    assert wakes == []
    denied = [r for r in caplog.records if r.getMessage() == "bot_access_denied"]
    assert len(denied) == 2 and denied[0].user_id == STRANGER
    await bot.session.close()


async def test_bot1_no_owner_configured_nobody_answered(aengine, caplog):
    ctx = context(aengine, [])
    ctx.owner_id = None
    dp = build_dispatcher(ctx)
    bot = fake_bot()
    with caplog.at_level(logging.WARNING, logger="hunter_core.access"):
        await dp.feed_update(bot, make_message_update(1, OWNER, "/start"))
    assert await outbox_rows(aengine) == []
    record = next(r for r in caplog.records if r.getMessage() == "bot_access_denied")
    assert record.user_id == OWNER and "TELEGRAM_OWNER_ID" in record.hint   # так владелец узнаёт свой id
    await bot.session.close()


async def test_owner_start_and_status_answered_once_per_update(aengine):
    wakes = []
    dp = build_dispatcher(context(aengine, wakes))
    bot = fake_bot()
    await dp.feed_update(bot, make_message_update(10, OWNER, "/start"))
    await dp.feed_update(bot, make_message_update(10, OWNER, "/start"))   # повторная доставка того же обновления
    await dp.feed_update(bot, make_message_update(11, OWNER, "/status"))
    rows = await outbox_rows(aengine)
    assert [r["dedup_key"] for r in rows] == ["reply:10", "reply:11"]
    assert all(r["chat_id"] == OWNER for r in rows)
    assert "/status" in rows[0]["payload"]["text"]
    assert "Состояние" in rows[1]["payload"]["text"] and "База: работает" in rows[1]["payload"]["text"]
    assert len(wakes) == 2
    await bot.session.close()


# ------------------------------------------------------------------ BOT-2
async def test_bot2_answer_within_3_seconds_during_long_background_job(aengine):
    from hunter_core.notify import OutboxSender

    sender = FakeSender()
    out = OutboxSender(aengine, sender, interval=10.0)
    loop_task = asyncio.create_task(out.run_forever())
    ctx = BotContext(engine=aengine, owner_id=OWNER, timezone="Asia/Tbilisi", report_time="07:00",
                     wake_sender=out.wake)
    dp = build_dispatcher(ctx)
    bot = fake_bot()

    release = asyncio.Event()

    async def long_job():
        # долгая фоновая задача: работа с базой и ожидание, в сумме дольше 3 секунд
        for _ in range(40):
            await asyncio.sleep(0.1)
        await release.wait()
        return None

    job = asyncio.create_task(JobRunner(aengine).run("collect", long_job))
    await asyncio.sleep(0.3)
    started = systime.monotonic()
    await dp.feed_update(bot, make_message_update(20, OWNER, "/status"))
    while not sender.sent and systime.monotonic() - started < 3:
        await asyncio.sleep(0.02)
    elapsed = systime.monotonic() - started
    assert sender.sent, "ответ не отправлен за 3 секунды"
    assert elapsed < 3
    assert not job.done()                      # фоновая задача всё ещё идёт
    release.set()
    await job
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)
    await bot.session.close()


# ------------------------------------------------------------------ BOT-3
async def test_bot3_restart_runs_recovery_before_scheduler_and_bot_answers(aengine, monkeypatch):
    clock = FakeClock()
    async with aengine.begin() as conn:   # «процесс упал» во время отправки
        await conn.execute(insert(outbox).values(dedup_key="report:1:header", chat_id=OWNER,
                                                 kind="report_header", payload={"text": "x"}, state="sending"))

    async def no_polling(self):
        return None

    monkeypatch.setattr(Application, "_poll", no_polling)
    sender = FakeSender()
    app = Application(config=load_app_config(), engine=aengine, sender=sender, owner_id=OWNER,
                      heartbeat=Heartbeat(None, user_agent="t"), bot_token="123:fake", clock=clock)
    await app.start()
    assert app.events == ["recovery", "outbox", "scheduler", "bot"]

    bot = fake_bot()
    await app.dispatcher.feed_update(bot, make_message_update(30, OWNER, "/start"))
    for _ in range(150):
        if sender.sent:
            break
        await asyncio.sleep(0.02)
    await app.stop()   # мягкая остановка: начатая отправка завершается и подтверждается
    await bot.session.close()

    rows = {r["dedup_key"]: r for r in await outbox_rows(aengine)}
    assert rows["report:1:header"]["state"] == "unconfirmed"   # не отправлено повторно
    assert rows["reply:30"]["state"] == "sent"
    assert [p["text"] for _, p in sender.sent] == [rows["reply:30"]["payload"]["text"]]
