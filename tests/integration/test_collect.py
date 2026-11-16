"""SRC-3…SRC-14: сбор и оперативный контроль источников (TEST_PLAN.md, раздел 4.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from hunter_core.storage.tables import fetch_runs, outbox, raw_items, source_state
from tests.fakes import FakeClock
from tests.fakes.sources import Router, config_with_sources, http_for, sample, tg_entry, tg_page
from vacancy_hunter.bot.texts import status_text
from vacancy_hunter.pipeline.collect import collect
from vacancy_hunter.sources.probe import probe_sources
from vacancy_hunter.storage.tables import vacancies, vacancy_sources

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
A = "https://t.me/s/chan_a"
B = "https://t.me/s/chan_b"


def posts(channel: str, numbers, *, age_days: float = 0.5):
    return [(n, NOW - timedelta(days=age_days, minutes=n), f"Вакансия {channel} №{n}\nPython, удалённо") for n in numbers]


async def count(engine, table, *where):
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(table).where(*where))).scalar_one()


async def state(engine, code):
    async with engine.connect() as conn:
        return dict((await conn.execute(select(source_state).where(source_state.c.code == code))).mappings().one())


async def alerts(engine):
    async with engine.connect() as conn:
        return (await conn.execute(select(outbox.c.dedup_key).where(outbox.c.kind == "alert"))).scalars().all()


class Alerter:
    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, kind: str, text: str):
        self.sent.append(kind)


async def run(engine, config, router, clock, **kw):
    http = http_for(router)
    try:
        return await collect(engine, config, clock=clock, http=http, **kw)
    finally:
        await http.aclose()


# ------------------------------------------------------------------ SRC-3
async def test_src3_repeated_collect_creates_no_new_rows(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    router = Router({A: tg_page("chan_a", posts("chan_a", range(1, 6)))})
    clock = FakeClock(NOW)
    first = await run(aengine, config, router, clock)
    assert first["new"] == 5
    clock.advance(hours=3)
    second = await run(aengine, config, router, clock)
    assert second["new"] == 0
    assert await count(aengine, vacancies) == 5
    assert await count(aengine, raw_items) == 5
    assert await count(aengine, vacancy_sources) == 5


async def test_src3_interval_respected_without_force(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1]))})
    clock = FakeClock(NOW)
    await run(aengine, config, router, clock)
    clock.advance(hours=1)
    assert (await run(aengine, config, router, clock))["skipped"] == 1   # не чаще раза в 3 часа (R5)
    assert len(router.requests) == 1


# ------------------------------------------------------------------ SRC-4, SRC-5
async def test_src4_failure_keeps_cursor_and_other_sources_collected(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a"), tg_entry("tg_b", "chan_b")])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1, 2])), B: tg_page("chan_b", posts("chan_b", [1]))})
    clock = FakeClock(NOW)
    await run(aengine, config, router, clock)
    cursor_before = (await state(aengine, "tg_a"))["cursor"]

    router.set(A, httpx.Response(500))
    router.set(B, tg_page("chan_b", posts("chan_b", [1, 2])))
    clock.advance(hours=3)
    stats = await run(aengine, config, router, clock)
    assert stats["failed"] == 1 and stats["success"] == 1
    a = await state(aengine, "tg_a")
    assert a["cursor"] == cursor_before and a["consecutive_failures"] == 1 and a["last_error"]
    async with aengine.connect() as conn:
        last = (await conn.execute(select(fetch_runs.c.status, fetch_runs.c.cursor_after)
                                   .where(fetch_runs.c.source_code == "tg_a").order_by(fetch_runs.c.id.desc()))).first()
    assert last.status == "failed" and last.cursor_after is None
    assert await count(aengine, vacancy_sources, vacancy_sources.c.source_code == "tg_b") == 2


async def test_src5_failure_then_success_collects_missed_posts(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    router = Router({A: tg_page("chan_a", posts("chan_a", range(1, 4)))})
    clock = FakeClock(NOW)
    await run(aengine, config, router, clock)
    router.set(A, httpx.ConnectError("down"))
    clock.advance(hours=3)
    await run(aengine, config, router, clock)
    # за время сбоя вышли посты 4–30; на первой странице видны только последние 20 (11–30)
    all_posts = posts("chan_a", range(1, 31))
    router.set(A, tg_page("chan_a", all_posts[10:]))
    router.set(A + "?before=11", tg_page("chan_a", all_posts[:10]))
    clock.advance(hours=3)
    await run(aengine, config, router, clock)
    async with aengine.connect() as conn:
        ids = sorted((await conn.execute(select(vacancy_sources.c.external_id))).scalars().all(),
                     key=lambda x: int(x.split("/")[1]))
    assert ids == [f"chan_a/{n}" for n in range(1, 31)]
    assert (await state(aengine, "tg_a"))["cursor"] == {"last_post": 30}
    assert A + "?before=11" in router.requests


# ------------------------------------------------------------------ SRC-6
async def test_src6_zero_posts_three_times_degrades_with_one_alert(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1]))})
    clock = FakeClock(NOW)
    alerter = Alerter()
    await run(aengine, config, router, clock, alert=alerter)
    router.set(A, tg_page("chan_a", []))   # разметка изменилась: постов не видно
    for _ in range(5):
        clock.advance(hours=3)
        await run(aengine, config, router, clock, alert=alerter, force=True)
    st = await state(aengine, "tg_a")
    assert st["health"] == "degraded"
    assert alerter.sent == ["source_empty:tg_a"]


async def test_degraded_after_five_failures_polled_every_12_hours_and_recovers(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    router = Router({A: httpx.Response(503)})
    clock = FakeClock(NOW)
    alerter = Alerter()
    for _ in range(5):
        await run(aengine, config, router, clock, alert=alerter)
        clock.advance(hours=3)
    assert (await state(aengine, "tg_a"))["health"] == "degraded"
    assert alerter.sent == ["source_degraded:tg_a"]
    assert (await run(aengine, config, router, clock, alert=alerter))["skipped"] == 1  # 12 часов ещё не прошло
    router.set(A, tg_page("chan_a", posts("chan_a", [1])))
    clock.advance(hours=12)
    await run(aengine, config, router, clock, alert=alerter)
    st = await state(aengine, "tg_a")
    assert st["health"] == "ok" and st["consecutive_failures"] == 0
    assert alerter.sent[-1] == "source_recovered:tg_a"


# ------------------------------------------------------------------ SRC-7
@pytest.mark.parametrize("status", [403, 451])
async def test_src7_forbidden_disables_source_alerts_and_stops_requests(aengine, status):
    config = config_with_sources([tg_entry("tg_a", "chan_a"), tg_entry("tg_b", "chan_b")])
    router = Router({A: httpx.Response(status), B: tg_page("chan_b", posts("chan_b", [1]))})
    clock = FakeClock(NOW)
    alerter = Alerter()
    stats = await run(aengine, config, router, clock, alert=alerter)
    assert stats["blocked"] == 1 and stats["success"] == 1
    assert router.requests.count(A) == 1                       # без повторов
    st = await state(aengine, "tg_a")
    assert (st["mode"], st["health"], st["mode_reason"]) == ("disabled", "blocked", "blocked")
    assert alerter.sent == ["source_blocked:tg_a"]
    clock.advance(hours=24)
    await run(aengine, config, router, clock, alert=alerter)   # конфигурация не снимает запрет сама
    assert router.requests.count(A) == 1
    assert (await state(aengine, "tg_a"))["mode"] == "disabled"


# ------------------------------------------------------------------ SRC-8
async def test_src8_posts_older_than_14_days_not_saved_for_analysis(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    page = tg_page("chan_a", posts("chan_a", [1, 2], age_days=20) + posts("chan_a", [3], age_days=13))
    router = Router({A: page})
    await run(aengine, config, router, FakeClock(NOW))
    async with aengine.connect() as conn:
        ids = (await conn.execute(select(vacancy_sources.c.external_id))).scalars().all()
        statuses = set((await conn.execute(select(vacancies.c.status))).scalars().all())
    assert ids == ["chan_a/3"]
    assert "pending_llm" not in statuses
    assert (await state(aengine, "tg_a"))["posts_total"] == 1


async def test_first_run_paging_stops_at_14_days(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    recent = posts("chan_a", range(21, 41), age_days=2)
    old = posts("chan_a", range(1, 21), age_days=20)
    router = Router({A: tg_page("chan_a", recent), A + "?before=21": tg_page("chan_a", old)})
    await run(aengine, config, router, FakeClock(NOW))
    assert await count(aengine, vacancies) == 20
    assert A + "?before=1" not in router.requests


# ------------------------------------------------------------------ SRC-9, SRC-10
async def test_src9_quiet_source_marks_quiet_only(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a", "quiet"), tg_entry("tg_b", "chan_b")])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1])), B: tg_page("chan_b", posts("chan_b", [1]))})
    await run(aengine, config, router, FakeClock(NOW))
    async with aengine.connect() as conn:
        rows = dict((await conn.execute(select(vacancies.c.primary_source_code, vacancies.c.quiet_only))).all())
    assert rows == {"tg_a": True, "tg_b": False}


@pytest.mark.parametrize("mode", ["waiting", "disabled"])
async def test_src10_waiting_and_disabled_not_polled(aengine, mode):
    config = config_with_sources([tg_entry("tg_a", "chan_a", mode)])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1]))})
    stats = await run(aengine, config, router, FakeClock(NOW))
    assert router.requests == [] and stats["sources"] == 0
    st = await state(aengine, "tg_a")              # виден в /status, но не опрашивался
    assert st["mode"] == mode and st["last_run_at"] is None


# ------------------------------------------------------------------ SRC-11, SRC-12
async def test_src11_new_channel_only_in_config_is_collected(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a"), tg_entry("tg_brand_new", "brand_new_channel")])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1])),
                     "https://t.me/s/brand_new_channel": tg_page("brand_new_channel",
                                                                 posts("brand_new_channel", [1, 2]))})
    await run(aengine, config, router, FakeClock(NOW))
    assert await count(aengine, vacancy_sources, vacancy_sources.c.source_code == "tg_brand_new") == 2


async def test_src12_posts_counter_grows_by_new_posts(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a")])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1, 2, 3]))})
    clock = FakeClock(NOW)
    await run(aengine, config, router, clock)
    assert (await state(aengine, "tg_a"))["posts_total"] == 3
    router.set(A, tg_page("chan_a", posts("chan_a", [1, 2, 3, 4, 5])))
    clock.advance(hours=3)
    await run(aengine, config, router, clock)
    assert (await state(aengine, "tg_a"))["posts_total"] == 5


async def test_real_samples_collected_from_all_three_adapters(aengine):
    config = config_with_sources([
        tg_entry("tg_it", "it_interns"),
        {"code": "hh_kz", "kind": "rss", "adapter": "hh_rss", "mode": "active",
         "url": "https://hh.kz/search/vacancy/rss?text={query}", "queries": ["python"]},
        {"code": "habr", "kind": "rss", "adapter": "habr_career_rss", "mode": "quiet",
         "url": "https://career.habr.com/vacancies/rss?q={query}", "queries": ["python"]},
    ])
    first_page = sample("telegram_it_interns.html")
    oldest = min(int(n) for n in __import__("re").findall(r'data-post="it_interns/(\d+)"', first_page))
    router = Router({"https://t.me/s/it_interns": first_page,
                     f"https://t.me/s/it_interns?before={oldest}": tg_page("it_interns", []),
                     "https://hh.kz/search/vacancy/rss?text=python": sample("hh_kz.xml"),
                     "https://career.habr.com/vacancies/rss?q=python": sample("habr_career.xml")})
    stats = await run(aengine, config, router, FakeClock(NOW))
    assert stats["success"] == 3
    assert await count(aengine, vacancies, vacancies.c.primary_source_code == "hh_kz",
                       vacancies.c.has_description.is_(False)) > 0
    assert await count(aengine, vacancies, vacancies.c.primary_source_code == "habr") > 0
    assert await count(aengine, vacancies, vacancies.c.primary_source_code == "tg_it") > 0
    assert await count(aengine, vacancies, vacancies.c.status != "new") == 0


# ------------------------------------------------------------------ SRC-13
async def test_src13_probe_reports_each_source_unavailable_does_not_crash():
    config = config_with_sources([
        tg_entry("tg_a", "chan_a"),
        {"code": "hh_ru", "kind": "rss", "adapter": "hh_rss", "mode": "disabled",
         "url": "https://hh.ru/search/vacancy/rss?text={query}", "queries": ["python"]},
        tg_entry("tg_down", "chan_down"),
    ])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1])),
                     "https://hh.ru/search/vacancy/rss?text=python": httpx.Response(451),
                     "https://t.me/s/chan_down": httpx.ConnectError("down")})
    results = await probe_sources(config.sources, transport=httpx.MockTransport(router))
    by_code = {r.code: r for r in results}
    assert set(by_code) == {"tg_a", "hh_ru", "tg_down"}
    assert by_code["tg_a"].ok and not by_code["hh_ru"].ok and not by_code["tg_down"].ok
    assert "451" in by_code["hh_ru"].detail


# ------------------------------------------------------------------ SRC-14
async def test_src14_status_shows_mode_health_last_success_and_error(aengine):
    config = config_with_sources([tg_entry("tg_a", "chan_a"), tg_entry("tg_b", "chan_b", "quiet")])
    router = Router({A: tg_page("chan_a", posts("chan_a", [1])), B: tg_page("chan_b", posts("chan_b", [1]))})
    clock = FakeClock(NOW)
    await run(aengine, config, router, clock)
    router.set(A, httpx.Response(502))
    clock.advance(hours=3)
    await run(aengine, config, router, clock)
    text = await status_text(aengine, "Asia/Tbilisi", clock)
    line_a = next(line for line in text.splitlines() if line.startswith("tg_a:"))
    assert "активен" in line_a and "успех 13.11 19:00" in line_a
    assert "сбоев подряд 1" in line_a and "502" in line_a
    line_b = next(line for line in text.splitlines() if line.startswith("tg_b:"))
    assert "тихий сбор" in line_b and "публикаций 1" in line_b
    assert "<b>Вакансии</b>: всего 2 (новые: 2)" in text


async def test_hung_source_hits_deadline_and_does_not_block_others(aengine):
    """Зависший источник (операция не поддаётся отмене) засчитывается как сбой; остальные собираются."""
    import asyncio

    from hunter_core.sources import SourceRunner
    from hunter_core.sources.runner import ControlPolicy
    from vacancy_hunter.pipeline.collect import make_store
    from vacancy_hunter.sources import build_adapters

    config = config_with_sources([tg_entry("tg_a", "chan_a"), tg_entry("tg_b", "chan_b")])
    adapters = build_adapters(config.sources)
    release = asyncio.Event()

    async def stuck(cursor, ctx):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()   # не поддаётся отмене, пока тест не отпустит
        raise RuntimeError("не должно дойти")

    adapters[0][0].fetch = stuck
    router = Router({B: tg_page("chan_b", posts("chan_b", [1]))})
    http = http_for(router)
    runner = SourceRunner(aengine, make_store({"tg_a": "telegram_preview", "tg_b": "telegram_preview"},
                                              FakeClock(NOW)),
                          policy=ControlPolicy(run_timeout=0.5), clock=FakeClock(NOW))
    outcomes = await asyncio.wait_for(runner.run_all(adapters, lambda _a: http), 10)
    release.set()
    await http.aclose()
    assert [(o.source, o.status) for o in outcomes] == [("tg_a", "failed"), ("tg_b", "success")]
    assert "не уложился" in (await state(aengine, "tg_a"))["last_error"]
