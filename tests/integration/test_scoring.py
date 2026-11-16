"""Скоринг в конвейере, версии профиля и правил, пересчёт: VER-1…VER-4 (TEST_PLAN.md, 4.6)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hunter_core.scheduler import JobRunner
from hunter_core.storage.tables import source_state
from tests.fakes import FakeClock
from tests.fakes.scoring import load_case, scoring_config
from vacancy_hunter.matching.versions import ensure_versions
from vacancy_hunter.pipeline.normalize import text_hash
from vacancy_hunter.pipeline.prefilter import PRELIMINARY_PENDING
from vacancy_hunter.pipeline.recalc import in_report_window, recalc
from vacancy_hunter.pipeline.score import score_pending
from vacancy_hunter.storage.tables import extractions, profile_versions, scores, scoring_versions, vacancies

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)          # 13:00 в Тбилиси — далеко от отчёта
CONFIG = scoring_config()


def changed_profile(config=CONFIG):
    """Тот же профиль без FastAPI: новая версия профиля, у примера 1 падает стек."""
    data = config.profile.model_dump()
    data["professional"]["skills_confirmed"]["Backend"] = ["rest api"]
    return replace(config, profile=type(config.profile).model_validate(data))


async def add_extracted(engine, case="example1", *, status="extracted", first_seen=NOW, title=None) -> int:
    v, facts, _ = load_case(case)
    text = v["description_text"] + f" #{first_seen.timestamp()}"
    async with engine.begin() as conn:
        await conn.execute(pg_insert(source_state).values(code="tg_a", kind="telegram", mode="active")
                           .on_conflict_do_nothing())
        await conn.execute(pg_insert(extractions).values(
            text_hash=text_hash(text), model="claude-haiku-5-5", prompt_version="p1-1.0", status="ok",
            parts_total=1, parts_done=1, chars_total=len(text), chars_analyzed=len(text), facts=facts,
            quote_check={"downgraded": []}, flags={},
        ).on_conflict_do_nothing())
        return (await conn.execute(insert(vacancies).values(
            title=title or v["title"], company_name=v["company_name"], status=status, has_description=True,
            description_text=text, description_hash=text_hash(text), primary_source_code="tg_a",
            first_seen_at=first_seen, last_seen_at=NOW - timedelta(hours=1), published_at=v["published_at"],
            offer_type="job",
        ).returning(vacancies.c.id))).scalar_one()


async def add_title_only(engine, title="Стажёр Python-разработчик") -> int:
    async with engine.begin() as conn:
        await conn.execute(pg_insert(source_state).values(code="hh_rss_kz", kind="rss", mode="active")
                           .on_conflict_do_nothing())
        return (await conn.execute(insert(vacancies).values(
            title=title, status="new", status_reason=PRELIMINARY_PENDING, has_description=False,
            primary_source_code="hh_rss_kz", first_seen_at=NOW, last_seen_at=NOW, published_at=NOW,
            work_format_src="remote", country_code="KZ",
        ).returning(vacancies.c.id))).scalar_one()


async def rows(engine, table, *where):
    async with engine.connect() as conn:
        return [dict(r) for r in (await conn.execute(select(table).where(*where).order_by(table.c.id))).mappings()]


async def one(engine, vid):
    return (await rows(engine, vacancies, vacancies.c.id == vid))[0]


# ------------------------------------------------------------------ версии
async def test_versions_created_once_and_new_on_change(aengine):
    clock = FakeClock(NOW)
    v1 = await ensure_versions(aengine, CONFIG, clock=clock)
    again = await ensure_versions(aengine, CONFIG, clock=clock)
    assert (v1.profile_no, v1.scoring_no) == (1, 1) and again.profile_id == v1.profile_id and not again.changed
    v2 = await ensure_versions(aengine, changed_profile(), clock=clock)
    assert v2.profile_no == 2 and v2.scoring_no == 1 and v2.changed
    current = await rows(aengine, profile_versions, profile_versions.c.is_current)
    assert [r["version_no"] for r in current] == [2]
    assert "contacts" not in current[0]["profile"]["professional"]          # контакты в снимок не попадают
    back = await ensure_versions(aengine, CONFIG, clock=clock)              # возврат к прежнему содержимому
    assert back.profile_no == 1 and back.changed
    rules = scoring_config(categories__excellent=86)
    assert (await ensure_versions(aengine, rules, clock=clock)).scoring_no == 2
    assert len(await rows(aengine, scoring_versions)) == 2


# ------------------------------------------------------------------ этап скоринга
async def test_scoring_stage_full_and_idempotent(aengine):
    vid = await add_extracted(aengine)
    stats = await score_pending(aengine, CONFIG, clock=FakeClock(NOW))
    assert stats == {"full:excellent": 1}
    v = await one(aengine, vid)
    assert v["status"] == "scored" and v["first_full_scored_at"] == NOW
    s = (await rows(aengine, scores, scores.c.vacancy_id == vid))[0]
    assert (s["kind"], s["state"], s["total"], s["category"], s["gate_result"]) == \
        ("full", "current", 90, "excellent", "passed")
    assert s["extraction_id"] is not None and s["breakdown"]["stack"] == 27
    assert await score_pending(aengine, CONFIG, clock=FakeClock(NOW)) == {}     # повтор ничего не меняет
    assert len(await rows(aengine, scores)) == 1


async def test_scoring_stage_rejected_gate_keeps_points(aengine):
    vid = await add_extracted(aengine)
    async with aengine.begin() as conn:
        ext = (await conn.execute(select(extractions))).mappings().one()
        facts = dict(ext["facts"])
        facts["is_offer"] = {"value": "no", "quote": "Python-разработчик Telegram-ботов", "confidence": "high"}
        facts["not_offer_kind"] = "ad"
        await conn.execute(update(extractions).values(facts=facts))
    await score_pending(aengine, CONFIG, clock=FakeClock(NOW))
    v = await one(aengine, vid)
    assert v["status"] == "rejected_gate" and v["status_reason"] == "gate:reliability"
    s = (await rows(aengine, scores))[0]
    assert s["gate_result"] == "rejected" and s["total"] > 0


async def test_scoring_stage_preliminary(aengine):
    vid = await add_title_only(aengine)
    stats = await score_pending(aengine, CONFIG, clock=FakeClock(NOW))
    assert stats == {"preliminary:consider": 1}
    v = await one(aengine, vid)
    assert v["status"] == "scored" and v["status_reason"] is None and v["first_full_scored_at"] is None
    assert v["offer_type"] == "internship"
    s = (await rows(aengine, scores))[0]
    assert s["kind"] == "preliminary" and s["total"] == 69 and s["extraction_id"] is None


async def test_first_full_scored_at_written_once(aengine):
    vid = await add_extracted(aengine)
    await score_pending(aengine, CONFIG, clock=FakeClock(NOW))
    async with aengine.begin() as conn:      # повторное извлечение после появления описания
        await conn.execute(update(vacancies).where(vacancies.c.id == vid).values(status="extracted"))
    later = NOW + timedelta(days=1)
    await score_pending(aengine, CONFIG, clock=FakeClock(later))
    assert (await one(aengine, vid))["first_full_scored_at"] == NOW
    history = await rows(aengine, scores)
    assert [r["state"] for r in history] == ["outdated", "current"] and history[0]["superseded_by"] == history[1]["id"]


# ------------------------------------------------------------------ VER-1
async def test_ver1_profile_change_pending_then_new_current_old_outdated(aengine):
    clock = FakeClock(NOW)
    scored = await add_extracted(aengine)
    reported = await add_extracted(aengine, first_seen=NOW - timedelta(hours=1))
    await score_pending(aengine, CONFIG, clock=clock)
    async with aengine.begin() as conn:
        await conn.execute(update(vacancies).where(vacancies.c.id == reported)
                           .values(status="reported", reported_at=NOW))
    before = {vid: await one(aengine, vid) for vid in (scored, reported)}

    new = changed_profile()
    versions = await ensure_versions(aengine, new, clock=clock)
    assert versions.marked == 2
    assert {r["state"] for r in await rows(aengine, scores)} == {"pending_recalc"}

    notes = []

    async def notify(kind, text):
        notes.append((kind, text))

    result = await recalc(aengine, new, clock=clock, notify=notify, sleep=lambda s: asyncio.sleep(0))
    assert result["recalculated"] == 2 and result["remaining"] == 0
    assert result.get("category_changed", 0) == 0          # 90 → 87: категория та же
    history = await rows(aengine, scores)
    old = [r for r in history if r["state"] == "outdated"]
    current = [r for r in history if r["state"] == "current"]
    assert len(old) == 2 and len(current) == 2
    assert all(r["profile_version_id"] == versions.profile_id for r in current)
    assert {r["superseded_by"] for r in old} == {r["id"] for r in current}
    assert all(r["total"] == 87 for r in current)              # без FastAPI стек 27 → 24
    for vid in (scored, reported):                              # даты и статус «показана» не меняются
        after = await one(aengine, vid)
        for key in ("first_seen_at", "published_at", "first_full_scored_at", "reported_at"):
            assert after[key] == before[vid][key]
    assert (await one(aengine, reported))["status"] == "reported"
    assert notes and notes[-1][0].startswith("recalc_done") and "пересчитано 2" in notes[-1][1]


async def test_recalc_includes_preliminary_scores(aengine):
    clock = FakeClock(NOW)
    vid = await add_title_only(aengine, "Python-разработчик")
    await score_pending(aengine, CONFIG, clock=clock)
    rules = scoring_config(preliminary__main=55)
    await ensure_versions(aengine, rules, clock=clock)
    await recalc(aengine, rules, clock=clock, sleep=lambda s: asyncio.sleep(0))
    current = await rows(aengine, scores, scores.c.vacancy_id == vid, scores.c.state == "current")
    assert current[0]["kind"] == "preliminary" and current[0]["breakdown"]["base"] == 55


# ------------------------------------------------------------------ VER-2
async def test_ver2_stop_in_the_middle_continues_with_rest(aengine):
    clock = FakeClock(NOW)
    ids = [await add_extracted(aengine, first_seen=NOW - timedelta(hours=i)) for i in range(5)]
    await score_pending(aengine, CONFIG, clock=clock)
    new = changed_profile()
    await ensure_versions(aengine, new, clock=clock)
    first = await recalc(aengine, new, clock=clock, batch=2, max_batches=1, sleep=lambda s: asyncio.sleep(0))
    assert first["recalculated"] == 2 and first["remaining"] == 3
    done = await rows(aengine, scores, scores.c.state == "current")
    assert {r["vacancy_id"] for r in done} == set(ids[:2])        # от свежих к старым
    # «перезапуск»: новая задача продолжает с оставшихся pending_recalc
    second = await recalc(aengine, new, clock=clock, batch=2, sleep=lambda s: asyncio.sleep(0))
    assert second["recalculated"] == 3 and second["remaining"] == 0
    assert len(await rows(aengine, scores, scores.c.state == "current")) == 5


# ------------------------------------------------------------------ VER-3
async def test_ver3_one_failure_keeps_old_score_others_recalculated(aengine):
    clock = FakeClock(NOW)
    good = await add_extracted(aengine)
    bad = await add_extracted(aengine, "example2", first_seen=NOW - timedelta(hours=2))
    await score_pending(aengine, CONFIG, clock=clock)
    async with aengine.begin() as conn:      # испорченные факты у одной вакансии
        v = (await conn.execute(select(vacancies).where(vacancies.c.id == bad))).mappings().one()
        await conn.execute(update(extractions).where(extractions.c.text_hash == v["description_hash"])
                           .values(facts={"broken": True}))
    new = changed_profile()
    await ensure_versions(aengine, new, clock=clock)
    notes = []

    async def notify(kind, text):
        notes.append(kind)

    for attempt in range(3):
        result = await recalc(aengine, new, clock=clock, notify=notify, sleep=lambda s: asyncio.sleep(0))
        assert result["failed"] == 1
        if attempt == 0:
            assert result["recalculated"] == 1
    failed = await rows(aengine, scores, scores.c.vacancy_id == bad)
    assert [r["state"] for r in failed] == ["recalc_failed"] and failed[0]["error_text"].startswith("[3]")
    assert failed[0]["total"] == 68                                   # прежний расчёт действует
    assert [r["state"] for r in await rows(aengine, scores, scores.c.vacancy_id == good)] == ["outdated", "current"]
    assert notes.count(f"recalc_failed:{bad}") == 1                    # после 3 ошибок подряд — одно оповещение


# ------------------------------------------------------------------ VER-4
def test_ver4_report_window():
    config = CONFIG                       # отчёт в 07:00 по Тбилиси (03:00 UTC)
    at = datetime.strptime("07:00", "%H:%M").time()
    assert in_report_window(datetime(2026, 11, 13, 2, 50, tzinfo=UTC), config, at)
    assert in_report_window(datetime(2026, 11, 13, 2, 45, tzinfo=UTC), config, at)
    assert not in_report_window(datetime(2026, 11, 13, 2, 44, tzinfo=UTC), config, at)
    assert not in_report_window(datetime(2026, 11, 13, 3, 0, tzinfo=UTC), config, at)


async def test_ver4_recalc_does_not_start_batches_before_report_and_does_not_block_it(aengine):
    clock = FakeClock(datetime(2026, 11, 13, 2, 50, tzinfo=UTC))
    await add_extracted(aengine)
    await score_pending(aengine, CONFIG, clock=clock)
    new = changed_profile()
    await ensure_versions(aengine, new, clock=clock)
    result = await recalc(aengine, new, clock=clock, sleep=lambda s: asyncio.sleep(0))
    assert result.get("paused_for_report") == 1 and result.get("recalculated", 0) == 0 and result["remaining"] == 1

    # Пересчёт держит только свою блокировку: отчёт запускается одновременно с ним.
    runner = JobRunner(aengine, clock=clock)
    release = asyncio.Event()
    started = asyncio.Event()

    async def long_recalc():
        started.set()
        await release.wait()

    async def report():
        return {"sent": True}

    task = asyncio.create_task(runner.run("recalc", long_recalc))
    await started.wait()
    assert await runner.run("report", report) == "success"       # не «skipped»: блокировки разные
    release.set()
    assert await task == "success"


async def test_process_runs_scoring_without_llm(aengine):
    from vacancy_hunter.pipeline.process import process

    vid = await add_extracted(aengine)
    result = await process(aengine, CONFIG, clock=FakeClock(NOW))
    assert result["scoring"] == {"full:excellent": 1}
    assert (await one(aengine, vid))["status"] == "scored"
    async with aengine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(scores))).scalar_one() == 1


# ------------------------------------------------------------------ замечания проверки кода
async def test_recalc_preliminary_does_not_override_status_from_full_score(aengine):
    """Вакансия оценена по заголовку, затем пришло описание и полная оценка; пересчёт обеих."""
    clock = FakeClock(NOW)
    vid = await add_title_only(aengine, "Python-разработчик")
    async with aengine.begin() as conn:      # заголовок: формат «офис» по ленте → предварительная отклонена
        await conn.execute(update(vacancies).where(vacancies.c.id == vid).values(work_format_src="office"))
    await score_pending(aengine, CONFIG, clock=clock)
    assert (await one(aengine, vid))["status"] == "rejected_gate"
    v, facts, _ = load_case("example1")
    text = v["description_text"]
    async with aengine.begin() as conn:      # появилось описание с удалённой работой → полная оценка прошла
        await conn.execute(pg_insert(extractions).values(
            text_hash=text_hash(text), model="claude-haiku-5-5", prompt_version="p1-1.0", status="ok",
            parts_total=1, parts_done=1, chars_total=len(text), chars_analyzed=len(text), facts=facts))
        await conn.execute(update(vacancies).where(vacancies.c.id == vid).values(
            status="extracted", has_description=True, description_text=text, description_hash=text_hash(text),
            work_format_src=None))
    await score_pending(aengine, CONFIG, clock=clock)
    assert (await one(aengine, vid))["status"] == "scored"
    rules = scoring_config(preliminary__main=55)
    await ensure_versions(aengine, rules, clock=clock)
    async with aengine.begin() as conn:      # полная оценка не пересчиталась (ошибка), предварительная — да
        await conn.execute(update(scores).where(scores.c.kind == "full").values(state="recalc_failed",
                                                                                 error_text="[1] x"))
        await conn.execute(update(vacancies).where(vacancies.c.id == vid).values(work_format_src="office"))
        await conn.execute(update(extractions).values(facts={"broken": True}))
    await recalc(aengine, rules, clock=clock, sleep=lambda s: asyncio.sleep(0))
    assert (await one(aengine, vid))["status"] == "scored"         # статус ведёт полная оценка


async def test_recalc_does_not_overwrite_status_changed_elsewhere(aengine):
    clock = FakeClock(NOW)
    vid = await add_extracted(aengine)
    await score_pending(aengine, CONFIG, clock=clock)
    new = changed_profile()
    await ensure_versions(aengine, new, clock=clock)
    async with aengine.begin() as conn:      # пока ждали пересчёта, дубль принёс описание: запись снова new
        await conn.execute(update(vacancies).where(vacancies.c.id == vid)
                           .values(status="new", status_reason="description_from_duplicate"))
    await recalc(aengine, new, clock=clock, sleep=lambda s: asyncio.sleep(0))
    v = await one(aengine, vid)
    assert (v["status"], v["status_reason"]) == ("new", "description_from_duplicate")


async def test_broken_record_does_not_block_scoring_of_others(aengine):
    clock = FakeClock(NOW)
    bad = await add_extracted(aengine, "example2", first_seen=NOW - timedelta(hours=3))
    good = await add_extracted(aengine)
    async with aengine.begin() as conn:
        v = (await conn.execute(select(vacancies).where(vacancies.c.id == bad))).mappings().one()
        await conn.execute(update(extractions).where(extractions.c.text_hash == v["description_hash"])
                           .values(facts={"broken": True}))
    stats = await score_pending(aengine, CONFIG, clock=clock)
    assert stats == {"error": 1, "full:excellent": 1}
    assert (await one(aengine, good))["status"] == "scored"
    broken = await one(aengine, bad)
    assert broken["status"] == "extracted" and broken["status_reason"].startswith("score_error")


async def test_completion_notice_sent_once_per_version_pair(aengine):
    clock = FakeClock(NOW)
    good = await add_extracted(aengine)
    bad = await add_extracted(aengine, "example2", first_seen=NOW - timedelta(hours=2))
    await score_pending(aengine, CONFIG, clock=clock)
    async with aengine.begin() as conn:
        v = (await conn.execute(select(vacancies).where(vacancies.c.id == bad))).mappings().one()
        await conn.execute(update(extractions).where(extractions.c.text_hash == v["description_hash"])
                           .values(facts={"broken": True}))
    new = changed_profile()
    await ensure_versions(aengine, new, clock=clock)
    notes = []

    async def notify(kind, text):
        notes.append((kind, text))

    for _ in range(3):                       # ошибочная оценка повторяется каждый запуск
        await recalc(aengine, new, clock=clock, notify=notify, sleep=lambda s: asyncio.sleep(0))
    done = [t for k, t in notes if k.startswith("recalc_done")]
    assert done == ["Пересчёт оценок завершён: пересчитано 1, изменили категорию 0, ошибок 1."]
    assert good


def test_refresh_matching_config_reads_changed_files(tmp_path):
    import shutil

    from vacancy_hunter.config import DEFAULT_CONFIG_DIR, load_app_config, refresh_matching_config

    for name in DEFAULT_CONFIG_DIR.glob("*.yaml"):
        shutil.copy(name, tmp_path / name.name)
    config = load_app_config(tmp_path)
    assert refresh_matching_config(config, tmp_path) is config               # файлы не менялись
    text = (tmp_path / "scoring.yaml").read_text(encoding="utf-8").replace("excellent: 85", "excellent: 86")
    (tmp_path / "scoring.yaml").write_text(text, encoding="utf-8")
    assert refresh_matching_config(config, tmp_path).scoring.categories.excellent == 86
    (tmp_path / "scoring.yaml").write_text("weights: oops\n", encoding="utf-8")   # ошибка — прежние правила
    assert refresh_matching_config(config, tmp_path) is config
