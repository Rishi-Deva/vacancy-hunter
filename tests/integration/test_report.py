"""REP-1…REP-17: отбор и формирование ежедневного отчёта (TEST_PLAN.md, 4.7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from hunter_core.storage.tables import outbox
from tests.fakes import FakeClock
from tests.fakes.report import CONFIG, NOW, FakeChecker, add_scored
from vacancy_hunter.reporting.actuality import Check
from vacancy_hunter.reporting.daily import build_daily_report, report_due
from vacancy_hunter.reporting.settings import save_setting
from vacancy_hunter.storage.tables import report_items, reports, score_overrides, scores, vacancies

pytestmark = pytest.mark.integration

OWNER = 111


async def build(engine, *, at=NOW, checker=None, **kw):
    return await build_daily_report(engine, CONFIG, checker=checker or FakeChecker(), owner_chat_id=OWNER,
                                    clock=FakeClock(at), **kw)


async def items(engine, report_id=None):
    async with engine.connect() as conn:
        q = select(report_items).order_by(report_items.c.block, report_items.c.position)
        if report_id:
            q = q.where(report_items.c.report_id == report_id)
        return [dict(r) for r in (await conn.execute(q)).mappings()]


async def messages(engine):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(outbox).order_by(outbox.c.id))).mappings()]


async def vac(engine, vid):
    async with engine.connect() as conn:
        return dict((await conn.execute(select(vacancies).where(vacancies.c.id == vid))).mappings().one())


async def set_setting(engine, name, value):
    async with engine.begin() as conn:
        await save_setting(conn, name, value, NOW)


# ------------------------------------------------------------------ REP-1, REP-2
async def test_rep1_limit_15_rest_stay_candidates(aengine):
    ids = [await add_scored(aengine, total=60 + i) for i in range(20)]
    result = await build(aengine)
    assert result["main"] == 15
    shown = {i["vacancy_id"] for i in await items(aengine)}
    assert shown == set(ids[5:])                              # 15 лучших по баллу
    nxt = await build(aengine, at=NOW + timedelta(days=1))    # остальные 5 — на следующий день
    assert {i["vacancy_id"] for i in await items(aengine, nxt["report_id"])} == set(ids[:5])


async def test_rep2_not_more_than_3_per_company(aengine):
    for i in range(5):
        await add_scored(aengine, total=80 + i, company="ООО Ромашка")
    other = await add_scored(aengine, total=55)
    result = await build(aengine)
    assert result["main"] == 4
    assert other in {i["vacancy_id"] for i in await items(aengine)}


# ------------------------------------------------------------------ REP-3, REP-4
async def test_rep3_nothing_suitable_summary_without_cards(aengine):
    result = await build(aengine)
    assert result["main"] == 0 and result["title_only"] == 0
    rows = await messages(aengine)
    assert [r["kind"] for r in rows] == ["report_header"]
    assert "Подходящих вакансий сегодня нет" in rows[0]["payload"]["text"]
    assert not rows[0]["payload"].get("disable_notification")          # сводка — со звуком


async def test_rep4_below_threshold_not_shown_and_not_backfilled(aengine):
    await add_scored(aengine, total=49)
    shown = await add_scored(aengine, total=50)
    result = await build(aengine)
    assert result["main"] == 1 and [i["vacancy_id"] for i in await items(aengine)] == [shown]


# ------------------------------------------------------------------ REP-5…REP-8: правило первого показа
async def test_rep5_shown_yesterday_not_today(aengine):
    vid = await add_scored(aengine, total=90)
    await build(aengine)
    second = await build(aengine, at=NOW + timedelta(days=1))
    assert second["main"] == 0
    v = await vac(aengine, vid)
    assert v["status"] == "reported" and v["reported_at"] == NOW


async def test_rep6_first_full_score_4_days_ago_not_shown(aengine):
    await add_scored(aengine, total=90, scored_ago=timedelta(days=4))
    assert (await build(aengine))["main"] == 0


async def test_rep7_published_15_days_ago_not_shown(aengine):
    await add_scored(aengine, total=90, published_ago=timedelta(days=15))
    assert (await build(aengine))["main"] == 0


async def test_rep8_recalculated_old_reported_vacancy_not_returned(aengine):
    vid = await add_scored(aengine, total=60, status="reported", reported_at=NOW - timedelta(days=5),
                           scored_ago=timedelta(days=6))
    before = await vac(aengine, vid)
    async with aengine.begin() as conn:      # пересчёт поднял балл
        await conn.execute(update(scores).where(scores.c.vacancy_id == vid).values(total=95, category="excellent"))
    assert (await build(aengine))["main"] == 0
    after = await vac(aengine, vid)
    assert after["reported_at"] == before["reported_at"]
    assert after["first_full_scored_at"] == before["first_full_scored_at"]


async def test_rep9_force_include_outside_window_not_in_daily(aengine):
    vid = await add_scored(aengine, total=40, scored_ago=timedelta(days=5))
    async with aengine.begin() as conn:
        await conn.execute(insert(score_overrides).values(vacancy_id=vid, kind="force_include", created_at=NOW))
    assert (await build(aengine))["main"] == 0


async def test_set_total_override_replaces_score_and_force_exclude_removes(aengine):
    low = await add_scored(aengine, total=40)
    excluded = await add_scored(aengine, total=95)
    async with aengine.begin() as conn:
        await conn.execute(insert(score_overrides).values(vacancy_id=low, kind="set_total", new_total=88,
                                                          created_at=NOW))
        await conn.execute(insert(score_overrides).values(vacancy_id=excluded, kind="force_exclude", created_at=NOW))
    await build(aengine)
    rows = await items(aengine)
    assert [(r["vacancy_id"], r["total_snapshot"]) for r in rows] == [(low, 88)]


# ------------------------------------------------------------------ REP-10, REP-11
async def test_rep10_shown_by_title_then_full_score_80_not_in_report(aengine):
    vid = await add_scored(aengine, total=80, title_only_reported_at=NOW - timedelta(days=1))
    assert (await build(aengine))["main"] == 0
    assert (await vac(aengine, vid))["reported_at"] is None


async def test_rep10_rescored_notice_once_per_day(aengine):
    from vacancy_hunter.reporting.rescored import notify_rescored

    await add_scored(aengine, total=80, title_only_reported_at=NOW - timedelta(days=1))
    await add_scored(aengine, total=60, title_only_reported_at=NOW - timedelta(days=1))
    clock = FakeClock(NOW)
    assert await notify_rescored(aengine, OWNER, timezone="Asia/Tbilisi", clock=clock) == 2
    await add_scored(aengine, total=90, title_only_reported_at=NOW - timedelta(days=1), scored_ago=timedelta(hours=1))
    assert await notify_rescored(aengine, OWNER, timezone="Asia/Tbilisi", clock=clock) == 0    # не чаще раза в сутки
    rows = [r for r in await messages(aengine) if r["dedup_key"].startswith("rescored:")]
    assert len(rows) == 1 and rows[0]["payload"]["text"].startswith("Дооценено 2 вакансий, из них 1 получили 70")
    assert rows[0]["payload"]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Показать"
    later = FakeClock(NOW + timedelta(days=1))
    assert await notify_rescored(aengine, OWNER, timezone="Asia/Tbilisi", clock=later) == 1    # новая — завтра


async def test_rep11_from_awaiting_description_scored_75_shown_once(aengine):
    vid = await add_scored(aengine, total=75, scored_ago=timedelta(hours=1))
    assert (await build(aengine))["main"] == 1
    assert (await build(aengine, at=NOW + timedelta(days=1)))["main"] == 0
    assert (await vac(aengine, vid))["status"] == "reported"


# ------------------------------------------------------------------ REP-12, REP-13: актуальность
async def test_rep12_closed_vacancy_gets_status_and_is_replaced(aengine):
    closed = await add_scored(aengine, total=90, source="tg_a")
    other = await add_scored(aengine, total=70)
    await set_setting(aengine, "max_cards", 1)
    result = await build(aengine, checker=FakeChecker({closed: Check("closed", reason="пост удалён")}))
    assert result["closed"] == 1
    assert [i["vacancy_id"] for i in await items(aengine)] == [other]
    v = await vac(aengine, closed)
    assert v["status"] == "closed" and v["closed_at"] == NOW


async def test_rep12_expired_deadline_closes_vacancy():
    from datetime import date

    from vacancy_hunter.reporting.actuality import ActualityChecker

    checker = ActualityChecker(None)
    today = date(2026, 11, 14)
    assert (await checker.check({"deadline": "2026-11-13", "telegram": None}, today)).state == "closed"
    soon = await checker.check({"deadline": "2026-11-16", "telegram": None}, today)
    assert soon.state == "unsupported" and soon.note == "срок отклика до 16.11.2026"


async def test_rep13_check_unavailable_shown_with_note(aengine):
    vid = await add_scored(aengine, total=90, source="tg_a")
    result = await build(aengine, checker=FakeChecker({vid: Check("unknown", note="актуальность не проверена")}))
    assert result["main"] == 1 and result["not_checked"] == 1
    card = next(r for r in await messages(aengine) if r["kind"] == "vacancy_card")
    assert "актуальность не проверена" in card["payload"]["text"]


async def test_telegram_check_by_post_page():
    import httpx

    from hunter_core.http import HttpClient, HttpPolicy
    from vacancy_hunter.reporting.actuality import ActualityChecker

    pages = {"ch/1": '<div class="tgme_widget_message_text">Вакансия</div>',
             "ch/2": '<div class="tgme_widget_message_error">Post not found</div>'}

    def handler(request):
        if request.url.path.endswith("/3"):
            raise httpx.ConnectError("нет сети")
        return httpx.Response(200, text=pages[request.url.path.strip("/")])

    http = HttpClient(["t.me"], user_agent="test", transport=httpx.MockTransport(handler),
                      policy=HttpPolicy(max_retries=0))
    checker = ActualityChecker(http)
    from datetime import date

    day = date(2026, 11, 14)
    assert (await checker.check({"telegram": "ch/1"}, day)).state == "open"
    assert (await checker.check({"telegram": "ch/2"}, day)).state == "closed"
    failed = await checker.check({"telegram": "ch/3"}, day)
    assert failed.state == "unknown" and failed.note == "актуальность не проверена"
    limited = ActualityChecker(http, max_requests=0)
    assert (await limited.check({"telegram": "ch/1"}, day)).state == "unknown"
    await http.aclose()


# ------------------------------------------------------------------ REP-14, REP-15
async def test_rep14_title_only_block_max_5_marked_outside_main_limit(aengine):
    for i in range(16):
        await add_scored(aengine, total=60 + i)
    for i in range(7):
        await add_scored(aengine, kind="preliminary", total=50 + i, offer_type="internship")
    await add_scored(aengine, kind="preliminary", total=45)            # ниже 50 — не показывается
    result = await build(aengine)
    assert result["main"] == 15 and result["title_only"] == 5
    title_cards = [r for r in await messages(aengine) if r["kind"] == "vacancy_card"][15:]
    assert len(title_cards) == 5
    assert all("Оценка только по заголовку, полное описание не проверено" in r["payload"]["text"]
               for r in title_cards)
    blocks = [i["block"] for i in await items(aengine)]
    assert blocks.count("title_only") == 5


async def test_rep15_three_offer_types_common_sort_and_breakdown(aengine):
    job = await add_scored(aengine, total=70, offer_type="job")
    intern = await add_scored(aengine, total=90, offer_type="internship")
    project = await add_scored(aengine, total=80, offer_type="project")
    await build(aengine)
    assert [i["vacancy_id"] for i in await items(aengine)] == [intern, project, job]
    rows = await messages(aengine)
    header = rows[0]["payload"]["text"]
    assert "Карточек: 3 (работа 1, стажировки 1, проекты 1)" in header
    labels = [r["payload"]["text"].split(" · ")[0] for r in rows[1:]]
    assert labels == ["<b>Стажировка", "<b>Разовый проект / фриланс", "<b>Работа"]
    assert all(r["payload"]["disable_notification"] is True and r["report_item_id"] for r in rows[1:])


# ------------------------------------------------------------------ REP-16, REP-17
async def test_rep16_threshold_change_applies_next_report_old_not_returned(aengine):
    await set_setting(aengine, "threshold", 70)
    fresh = await add_scored(aengine, total=60, scored_ago=timedelta(hours=1))
    old = await add_scored(aengine, total=60, scored_ago=timedelta(days=3, hours=1))
    assert (await build(aengine))["main"] == 0
    await set_setting(aengine, "threshold", 50)
    nxt = await build(aengine, at=NOW + timedelta(days=1))
    shown = {i["vacancy_id"] for i in await items(aengine, nxt["report_id"])}
    assert fresh in shown and old not in shown
    assert (await vac(aengine, old))["status"] == "scored"           # статус не меняется


async def test_rep17_quiet_source_vacancy_not_shown(aengine):
    await add_scored(aengine, total=99, quiet=True)
    assert (await build(aengine))["main"] == 0


# ------------------------------------------------------------------ один отчёт в день, пауза, время
async def test_one_daily_report_per_date_and_pause(aengine):
    await add_scored(aengine, total=80)
    first = await build(aengine)
    assert (await build(aengine)) == {"skipped": "exists"}
    async with aengine.connect() as conn:
        assert (await conn.execute(select(reports.c.status).where(reports.c.id == first["report_id"]))).scalar() \
            == "ready"
    await set_setting(aengine, "paused", True)
    assert (await build(aengine, at=NOW + timedelta(days=1))) == {"skipped": "paused"}


async def test_report_due_window_and_settings(aengine):
    at = lambda h, m=0: FakeClock(datetime(2026, 11, 14, h - 4, m, tzinfo=UTC))  # noqa: E731 — Тбилиси UTC+4
    assert not await report_due(aengine, CONFIG, at(6, 59))
    assert await report_due(aengine, CONFIG, at(7, 0))
    assert await report_due(aengine, CONFIG, at(13, 59))          # догоняющий запуск после перезапуска
    assert not await report_due(aengine, CONFIG, at(14, 0))
    await set_setting(aengine, "report_time", "09:30")
    assert not await report_due(aengine, CONFIG, at(9, 25))
    assert await report_due(aengine, CONFIG, at(9, 30))
    await set_setting(aengine, "paused", True)
    assert not await report_due(aengine, CONFIG, at(9, 30))


async def test_dry_run_writes_nothing(aengine):
    await add_scored(aengine, total=80)
    result = await build(aengine, dry_run=True)
    assert result["dry_run"] and len(result["cards"]) == 1
    assert await items(aengine) == [] and await messages(aengine) == []


# ------------------------------------------------------------------ замечания проверки кода
async def test_no_owner_no_report_and_no_show_marks(aengine):
    vid = await add_scored(aengine, total=90)
    result = await build_daily_report(aengine, CONFIG, checker=FakeChecker(), owner_chat_id=None,
                                      clock=FakeClock(NOW))
    assert result == {"skipped": "no_owner"}
    assert (await vac(aengine, vid))["reported_at"] is None and await items(aengine) == []


async def test_report_attempts_not_more_often_than_15_minutes(aengine):
    from vacancy_hunter.reporting.daily import claim_attempt

    assert await claim_attempt(aengine, FakeClock(NOW))
    assert not await claim_attempt(aengine, FakeClock(NOW + timedelta(minutes=14)))
    assert await claim_attempt(aengine, FakeClock(NOW + timedelta(minutes=15)))


class RejectingChecker(FakeChecker):
    """Пока идёт проверка актуальности, владелец отклоняет вакансию."""

    def __init__(self, engine, vid):
        super().__init__()
        self.engine, self.vid = engine, vid

    async def check(self, candidate, today):
        if candidate["vacancy"]["id"] == self.vid:
            async with self.engine.begin() as conn:
                await conn.execute(update(vacancies).where(vacancies.c.id == self.vid).values(user_state="rejected"))
        return await super().check(candidate, today)


async def test_vacancy_rejected_during_check_not_marked_shown(aengine):
    vid = await add_scored(aengine, total=90)
    other = await add_scored(aengine, total=80)
    result = await build(aengine, checker=RejectingChecker(aengine, vid))
    assert result["main"] == 1 and result["changed_meanwhile"] == 1
    assert [i["vacancy_id"] for i in await items(aengine)] == [other]
    v = await vac(aengine, vid)
    assert v["reported_at"] is None and v["status"] == "scored"
    cards = [r for r in await messages(aengine) if r["kind"] == "vacancy_card"]
    assert len(cards) == 1
