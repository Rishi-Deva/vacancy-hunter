"""DUP-1…DUP-7: дедупликация (TEST_PLAN.md, раздел 4.4; проверочные пары TECHNICAL_SPEC.md, раздел 7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from hunter_core.storage.tables import source_state
from tests.fakes import FakeClock
from vacancy_hunter.config import load_app_config
from vacancy_hunter.pipeline.normalize import canonical_url, norm_company, norm_title, text_hash
from vacancy_hunter.pipeline.process import process
from vacancy_hunter.storage.tables import vacancies, vacancy_sources

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
CONFIG = load_app_config()
TEXT = ("Ищем Python-разработчика для поддержки Telegram-ботов. Обязательно: Python, aiogram, PostgreSQL. "
        "Опыт от 1 года. Работа удалённая, можно из любой страны.")


async def add(engine, *, source="tg_a", ext, title="Python-разработчик", company=None, text=TEXT, url=None,
              has_description=True, quiet=False, status="new", seen=NOW, **extra) -> int:
    async with engine.begin() as conn:
        await conn.execute(_upsert_source(source))
        vid = (await conn.execute(insert(vacancies).values(
            title=title, title_norm=norm_title(title), company_name=company, company_norm=norm_company(company),
            description_text=text if has_description else None,
            description_hash=text_hash(text) if has_description else None,
            has_description=has_description, canonical_url=canonical_url(url), quiet_only=quiet, status=status,
            published_at=seen, first_seen_at=seen, last_seen_at=seen, **extra,
        ).returning(vacancies.c.id))).scalar_one()
        await conn.execute(insert(vacancy_sources).values(vacancy_id=vid, source_code=source, external_id=ext,
                                                          first_seen_at=seen))
    return vid


def _upsert_source(code):
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return pg_insert(source_state).values(code=code, kind="telegram", mode="active").on_conflict_do_nothing()


async def get(engine, vid):
    async with engine.connect() as conn:
        return dict((await conn.execute(select(vacancies).where(vacancies.c.id == vid))).mappings().one())


async def run(engine, clock=None):
    return await process(engine, CONFIG, clock=clock or FakeClock(NOW))


# ------------------------------------------------------------------ DUP-1
async def test_dup1_site_vacancy_and_channel_retelling_with_link(aengine):
    site = await add(aengine, source="habr", ext="1", url="https://career.habr.com/vacancies/1000168585",
                     company="IBS", text="Компания IBS ищет QA. Можно удалённо. Требуемые навыки: Python.")
    post = await add(aengine, source="tg_a", ext="chan/5",
                     url="https://career.habr.com/vacancies/1000168585?utm_source=telegram",
                     text="🔥 Отличная вакансия QA в IBS, подробности по ссылке. Пересказ своими словами для канала.")
    await run(aengine)
    p = await get(aengine, post)
    assert (p["status"], p["duplicate_of"], p["status_reason"]) == ("duplicate", site, "dedup_level_1")
    async with aengine.connect() as conn:
        links = (await conn.execute(select(vacancy_sources.c.source_code)
                                    .where(vacancy_sources.c.vacancy_id == site))).scalars().all()
    assert sorted(links) == ["habr", "tg_a"]          # источник дубля добавлен оригиналу


# ------------------------------------------------------------------ DUP-2
async def test_dup2_same_post_in_two_channels(aengine):
    a = await add(aengine, source="tg_a", ext="a/1", url="https://t.me/a/1", text=TEXT + " #вакансия")
    b = await add(aengine, source="tg_b", ext="b/7", url="https://t.me/b/7", text="  " + TEXT.upper() + " 🚀")
    await run(aengine)
    assert (await get(aengine, b))["duplicate_of"] == a
    assert (await get(aengine, b))["status_reason"] == "dedup_level_2"
    assert (await get(aengine, a))["status"] == "pending_llm"


# ------------------------------------------------------------------ DUP-3, DUP-4
async def test_dup3_similar_title_same_company(aengine):
    a = await add(aengine, source="hh", ext="1", title="Python-разработчик", company="ООО «Пример»",
                  text="Описание первой публикации вакансии, достаточно длинное для правил. " * 2)
    b = await add(aengine, source="tg_a", ext="c/2", title="Разработчик Python (удалённо)", company="Пример",
                  text="Другой текст той же вакансии, опубликованный в канале со своими формулировками. " * 2)
    await run(aengine)
    vb = await get(aengine, b)
    assert (vb["duplicate_of"], vb["status_reason"]) == (a, "dedup_level_3")


async def test_dup4_two_different_vacancies_same_company(aengine):
    a = await add(aengine, source="hh", ext="1", title="QA-инженер", company="Пример", text="Текст про QA. " * 10)
    b = await add(aengine, source="hh", ext="2", title="Python-разработчик", company="Пример",
                  text="Текст про Python. " * 10)
    await run(aengine)
    assert (await get(aengine, a))["status"] == "pending_llm"
    assert (await get(aengine, b))["status"] == "pending_llm"


async def test_level3_not_applied_without_company_and_outside_30_days(aengine):
    a = await add(aengine, ext="1", title="Python-разработчик", text="Первый текст. " * 10,
                  seen=NOW - timedelta(days=40))
    b = await add(aengine, ext="2", title="Python-разработчик", company="Пример", text="Второй текст. " * 10)
    c = await add(aengine, ext="3", title="Разработчик Python", text="Третий текст. " * 10)
    await add(aengine, ext="4", title="Python разработчик", company="Пример", text="Старая публикация. " * 10,
              seen=NOW - timedelta(days=31), status="pending_llm")
    await run(aengine)
    for v in (a, b, c):
        assert (await get(aengine, v))["status"] != "duplicate"


# ------------------------------------------------------------------ DUP-5
async def test_dup5_description_moved_to_original_without_description(aengine):
    hh = await add(aengine, source="hh", ext="777", title="Python-разработчик", company="Пример",
                   url="https://hh.kz/vacancy/777", has_description=False, status="awaiting_description")
    post = await add(aengine, source="tg_a", ext="c/9", url="https://hh.kz/vacancy/777?from=share", text=TEXT)
    await run(aengine)
    original = await get(aengine, hh)
    assert (await get(aengine, post))["duplicate_of"] == hh
    assert original["has_description"] is True and original["description_text"] == TEXT
    assert original["status"] == "pending_llm"          # оригинал прошёл полный конвейер в той же строке
    async with aengine.connect() as conn:
        assert len((await conn.execute(select(vacancies.c.id).where(vacancies.c.status != "duplicate"))).all()) == 1


# ------------------------------------------------------------------ DUP-6
async def test_dup6_active_duplicate_clears_quiet_only(aengine):
    quiet = await add(aengine, source="tg_q", ext="q/1", url="https://t.me/q/1", quiet=True)
    await add(aengine, source="tg_a", ext="a/1", url="https://t.me/a/1", quiet=False)
    await run(aengine)
    assert (await get(aengine, quiet))["quiet_only"] is False


async def test_quiet_duplicate_keeps_original_active(aengine):
    active = await add(aengine, source="tg_a", ext="a/1", url="https://t.me/a/1", quiet=False)
    await add(aengine, source="tg_q", ext="q/1", url="https://t.me/q/1", quiet=True)
    await run(aengine)
    assert (await get(aengine, active))["quiet_only"] is False


# ------------------------------------------------------------------ DUP-7
async def test_dup7_duplicate_of_shown_vacancy_not_shown_again(aengine):
    shown_at = NOW - timedelta(days=2)
    shown = await add(aengine, ext="a/1", url="https://company.ru/jobs/1", status="reported",
                      seen=NOW - timedelta(days=3), reported_at=shown_at, first_full_scored_at=shown_at,
                      user_state="rejected")
    again = await add(aengine, source="tg_b", ext="b/2", url="https://company.ru/jobs/1",
                      text="Повторная публикация той же вакансии через пару дней. " * 3)
    await run(aengine)
    original = await get(aengine, shown)
    assert (await get(aengine, again))["status"] == "duplicate"
    assert original["status"] == "reported" and original["reported_at"] == shown_at
    assert original["user_state"] == "rejected"


async def test_process_is_idempotent(aengine):
    await add(aengine, ext="a/1", url="https://t.me/a/1")
    first = await run(aengine)
    second = await run(aengine)
    assert first["dedup_prefilter"] == {"pending_llm": 1}
    assert second["dedup_prefilter"] == {}
