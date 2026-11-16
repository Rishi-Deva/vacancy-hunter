"""UI-1…UI-6: команды и кнопки бота (TEST_PLAN.md, 4.7)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from hunter_core.storage.tables import outbox
from tests.fakes import FakeClock, fake_bot, make_callback_update, make_message_update
from tests.fakes.report import CONFIG, NOW, FakeChecker, add_scored
from vacancy_hunter.bot import BotContext, build_dispatcher
from vacancy_hunter.reporting.daily import build_daily_report
from vacancy_hunter.reporting.settings import load_settings
from vacancy_hunter.storage.tables import feedback_events, vacancies

pytestmark = pytest.mark.integration

OWNER = 111


class Bot:
    def __init__(self, engine):
        self.engine = engine
        self.wakes = []
        self.ctx = BotContext(engine=engine, owner_id=OWNER, timezone="Asia/Tbilisi", report_time="07:00",
                              wake_sender=lambda: self.wakes.append(1), clock=FakeClock(NOW), config=CONFIG)
        self.dp = build_dispatcher(self.ctx)
        self.bot = fake_bot()
        self.n = 100

    async def text(self, text: str):
        self.n += 1
        return await self.dp.feed_update(self.bot, make_message_update(self.n, OWNER, text))

    async def press(self, data: str):
        """Нажатие кнопки; возвращает текст всплывающего ответа."""
        self.n += 1
        result = await self.dp.feed_update(self.bot, make_callback_update(self.n, OWNER, data))
        return getattr(result, "text", None)

    async def last_reply(self) -> str:
        async with self.engine.connect() as conn:
            return (await conn.execute(select(outbox.c.payload).order_by(outbox.c.id.desc()).limit(1))).scalar()["text"]

    async def close(self):
        await self.bot.session.close()


@pytest.fixture
async def ui(aengine):
    b = Bot(aengine)
    yield b
    await b.close()


async def outbox_rows(engine):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(outbox).order_by(outbox.c.id))).mappings()]


# ------------------------------------------------------------------ UI-1
async def test_ui1_today_before_and_after_report(ui, aengine):
    await ui.text("/today")
    assert "ещё не сформирован" in await ui.last_reply() and "07:00" in await ui.last_reply()
    job = await add_scored(aengine, total=90, offer_type="job")
    await add_scored(aengine, total=80, offer_type="internship")
    await build_daily_report(aengine, CONFIG, checker=FakeChecker(), owner_chat_id=OWNER, clock=FakeClock(NOW))
    async with aengine.begin() as conn:      # одно сообщение отчёта «не подтверждено»
        await conn.execute(update(outbox).where(outbox.c.kind == "vacancy_card").values(state="unconfirmed"))
    before = len(await outbox_rows(aengine))
    await ui.text("/today")
    rows = (await outbox_rows(aengine))[before:]
    copies = [r for r in rows if ":req:" in r["dedup_key"]]
    assert [r["kind"] for r in copies] == ["report_header", "vacancy_card", "vacancy_card"]   # полный состав
    assert rows[-1]["payload"]["text"] == "Показать по типу:"
    answer = await ui.press("today:job")                               # фильтр меняет только показ
    filtered = [r for r in await outbox_rows(aengine) if r["dedup_key"].endswith(f":req:ccb{ui.n}")]
    assert answer == "Отчёт: работа"
    assert [r["kind"] for r in filtered] == ["report_header", "vacancy_card"]
    assert filtered[1]["payload"]["text"].startswith("<b>Работа ·")     # только карточка вида «работа»
    assert job


# ------------------------------------------------------------------ UI-2
@pytest.mark.parametrize("name,value,accepted", [
    ("threshold", "49", False), ("threshold", "50", True), ("threshold", "90", True), ("threshold", "91", False),
    ("max_cards", "0", False), ("max_cards", "1", True), ("max_cards", "15", True), ("max_cards", "16", False),
])
async def test_ui2_settings_accept_only_valid_values(ui, aengine, name, value, accepted):
    before = await load_settings(aengine, CONFIG)
    assert await ui.press(f"set:{name}") == "Жду значение"
    await ui.text(value)
    reply = await ui.last_reply()
    after = await load_settings(aengine, CONFIG)
    if accepted:
        assert getattr(after, name) == int(value)
        assert reply.startswith("Порог показа: " + value) if name == "threshold" else reply == f"Карточек в отчёте: {value}"
    else:
        assert after == before and "Прежнее значение сохранено" in reply


async def test_settings_report_time_and_unknown_text(ui, aengine):
    await ui.text("просто текст")
    assert "Не знаю такой команды" in await ui.last_reply()
    await ui.press("set:report_time")
    await ui.text("08:35")
    assert await ui.last_reply() == "Отчёт будет приходить в 08:35 по времени Грузии"
    assert (await load_settings(aengine, CONFIG)).report_time == "08:35"
    await ui.text("/settings")
    assert "Время отчёта: 08:35" in await ui.last_reply()


# ------------------------------------------------------------------ UI-3, UI-4
async def events(engine, vid):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(feedback_events).where(feedback_events.c.vacancy_id == vid)
                                                     .order_by(feedback_events.c.id))).mappings()]


async def test_ui3_double_interested_one_event(ui, aengine):
    vid = await add_scored(aengine, total=80)
    assert await ui.press(f"fb:interested:{vid}") == "Отмечено: интересно"
    assert await ui.press(f"fb:interested:{vid}") == "Уже отмечено"
    rows = await events(aengine, vid)
    assert [r["event"] for r in rows] == ["interested"] and rows[0]["score_id"] is not None


async def test_ui4_interested_rejected_cleared_three_events(ui, aengine):
    vid = await add_scored(aengine, total=80)
    await ui.press(f"fb:interested:{vid}")
    assert await ui.press(f"rj:{vid}") == "Выберите причину"
    assert "Причина отказа" in await ui.last_reply()
    assert await ui.press(f"rjr:stack:{vid}") == "Отклонено"
    assert await ui.press(f"fb:cleared:{vid}") == "Отметка снята"
    rows = await events(aengine, vid)
    assert [(r["event"], r["reason_code"]) for r in rows] == [("interested", None), ("rejected", "stack"),
                                                              ("cleared", None)]
    async with aengine.connect() as conn:
        assert (await conn.execute(select(vacancies.c.user_state).where(vacancies.c.id == vid))).scalar() == "none"


async def test_rejected_vacancy_excluded_from_report(ui, aengine):
    vid = await add_scored(aengine, total=95)
    await ui.press(f"rjr:company:{vid}")
    result = await build_daily_report(aengine, CONFIG, checker=FakeChecker(), owner_chat_id=OWNER,
                                      clock=FakeClock(NOW))
    assert result["main"] == 0


# ------------------------------------------------------------------ UI-5
async def test_ui5_stats_by_offer_type(ui, aengine):
    for t in ("job", "internship", "project"):
        vid = await add_scored(aengine, total=80, offer_type=t)
        await ui.press(f"fb:applied:{vid}")
    await ui.text("/stats")
    text = await ui.last_reply()
    for title in ("<b>Работа</b>", "<b>Стажировки</b>", "<b>Разовые проекты</b>"):
        assert text.count(title) == 2                                 # за 7 и за 30 дней
    assert "откликнулся 1" in text and "За 7 дней" in text and "За 30 дней" in text
    await ui.press("list:applied")
    assert (await ui.last_reply()).count("<a href=") == 3
    await ui.press("stats:project")
    assert "<b>Работа</b>" not in await ui.last_reply()


# ------------------------------------------------------------------ UI-6
async def test_ui6_pause_stops_report_not_pipeline(ui, aengine):
    await add_scored(aengine, total=90)
    assert await ui.press("set:paused:on") == "Отчёты приостановлены. Сбор и обработка продолжаются"
    result = await build_daily_report(aengine, CONFIG, checker=FakeChecker(), owner_chat_id=OWNER,
                                      clock=FakeClock(NOW))
    assert result == {"skipped": "paused"}
    await ui.press("set:paused:off")
    assert not (await load_settings(aengine, CONFIG)).paused


# ------------------------------------------------------------------ прочие кнопки
async def test_details_later_and_stub_commands(ui, aengine):
    vid = await add_scored(aengine, total=80)
    await ui.press(f"more:{vid}")
    assert "подробно" in await ui.last_reply() and "Формат:" in await ui.last_reply()
    assert await ui.press(f"later:letter:{vid}") == "Появится на следующем этапе."
    await ui.text("/search python")
    assert await ui.last_reply() == "Появится на следующем этапе."
    await ui.text("/start")
    assert "07:00" in await ui.last_reply() and "Сейчас работают" not in await ui.last_reply()


async def test_settings_input_expires(ui, aengine):
    await ui.press("set:threshold")
    ui.ctx.clock = FakeClock(NOW + timedelta(minutes=11))
    await ui.text("60")
    assert "Не знаю такой команды" in await ui.last_reply()
    assert (await load_settings(aengine, CONFIG)).threshold == 50


# ------------------------------------------------------------------ замечания проверки кода
async def test_parallel_double_press_one_event(ui, aengine):
    import asyncio

    vid = await add_scored(aengine, total=80)
    answers = await asyncio.gather(ui.press(f"fb:applied:{vid}"), ui.press(f"fb:applied:{vid}"))
    assert sorted(answers) == ["Отмечено: откликнулся", "Уже отмечено"]
    assert [e["event"] for e in await events(aengine, vid)] == ["applied"]


async def test_long_list_fits_telegram_limit(ui, aengine):
    for i in range(30):
        vid = await add_scored(aengine, total=80, title="Очень длинное название вакансии " * 10,
                               company="Компания с длинным названием " * 5 + str(i))
        await ui.press(f"fb:interested:{vid}")
    await ui.press("list:interested")
    text = await ui.last_reply()
    assert len(text) <= 4096 and "и ещё" in text


async def test_long_details_cut_by_whole_lines(ui, aengine):
    vid = await add_scored(aengine, total=80)
    async with aengine.begin() as conn:
        from vacancy_hunter.storage.tables import scores

        row = (await conn.execute(select(scores.c.explanation).where(scores.c.vacancy_id == vid))).scalar()
        row["skills"]["required"] = [{"name": "навык&<" + "x" * 60, "canonical": None, "status": "missing",
                                      "via": None}] * 30
        row["tasks"] = [{"text": "задача " * 15, "type": "other", "core": True, "covered": False,
                         "projects": []}] * 40
        await conn.execute(update(scores).where(scores.c.vacancy_id == vid).values(explanation=row))
    await ui.press(f"more:{vid}")
    text = await ui.last_reply()
    assert len(text) <= 4096 and text.count("<b>") == text.count("</b>")
    assert "&am…" not in text


async def test_handler_failure_still_answers(ui, aengine, monkeypatch):
    from vacancy_hunter.reporting import feedback

    async def broken(*a, **kw):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(feedback, "record", broken)
    assert await ui.press("fb:interested:1") == "Ошибка, попробуйте позже"
    assert await ui.press("set:paused:maybe") == "Неверная кнопка"
