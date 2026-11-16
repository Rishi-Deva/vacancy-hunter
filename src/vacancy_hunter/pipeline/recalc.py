"""Пересчёт оценок без LLM (TECHNICAL_SPEC.md, раздел 19; DATABASE_SCHEMA.md, раздел 6.2).

- берёт оценки `pending_recalc` и `recalc_failed` пакетами по 200, каждый пакет — своя транзакция,
  от свежих вакансий к старым (`first_seen_at`); пауза 1 секунда между пакетами;
- новая строка `current` по текущим версиям; прежняя → `outdated` (история сохраняется);
- отдельного счётчика нет: после перезапуска задача продолжает с оставшихся `pending_recalc` (VER-2);
- ошибка одной оценки → `recalc_failed`, прежний расчёт действует, остальные пересчитываются (VER-3);
  после 3 ошибок подряд по одной вакансии — оповещение владельцу;
- блокировок сбора, обработки и отчёта не берёт; за 15 минут до времени отчёта новые
  пакеты не начинаются (VER-4);
- статусы `scored` и `rejected_gate` могут поменяться по итогам, `reported` не меняется;
  даты первого обнаружения, публикации, первой полной оценки и показа не меняются.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.scheduler.jobs import local_now
from hunter_core.storage.tables import app_state
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.matching import MatchProfile
from vacancy_hunter.matching.versions import ensure_versions
from vacancy_hunter.pipeline.score import compute, gate_reason, has_active_full, store_score
from vacancy_hunter.storage.tables import extractions, scores, vacancies

log = logging.getLogger("vacancy_hunter.recalc")

BATCH = 200
PAUSE_SECONDS = 1.0
FAILURES_TO_ALERT = 3
DONE_KEY = "recalc_done_versions"     # о какой паре версий уже сообщено
REPORT_TIME_KEY = "report_time"      # значение из Telegram (/settings, шаг A7) приоритетнее файла

Notify = Callable[[str, str], Awaitable[object]]


async def report_time(engine: AsyncEngine, config: AppConfig) -> time:
    async with engine.connect() as conn:
        value = (await conn.execute(select(app_state.c.value).where(app_state.c.key == REPORT_TIME_KEY))).scalar()
    try:
        return time.fromisoformat(value) if isinstance(value, str) and value else \
            time.fromisoformat(config.schedule.report_time)
    except ValueError:
        return time.fromisoformat(config.schedule.report_time)


def in_report_window(now: datetime, config: AppConfig, at: time) -> bool:
    """Последние N минут перед отчётом: новые пакеты не начинаются."""
    local = local_now(lambda: now, config.schedule.timezone)
    window = timedelta(minutes=config.schedule.recalc_pause_before_report_minutes)
    for day in (local.date(), local.date() + timedelta(days=1)):     # отчёт сразу после полуночи
        start_dt = datetime.combine(day, at, tzinfo=local.tzinfo)
        if start_dt - window <= local < start_dt:
            return True
    return False


def _failures(error_text: str | None) -> int:
    m = re.match(r"\[(\d+)\]", error_text or "")
    return int(m.group(1)) if m else 0


async def pending_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(scores)
                                   .where(scores.c.state == "pending_recalc"))).scalar_one()


async def recalc(engine: AsyncEngine, config: AppConfig, *, clock: Clock = utcnow, notify: Notify | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, batch: int = BATCH,
                 pause: float = PAUSE_SECONDS, max_batches: int | None = None) -> dict:
    versions = await ensure_versions(engine, config, clock=clock)
    profile = MatchProfile.from_config(config)
    at = await report_time(engine, config)
    stats: Counter = Counter()
    seen: set[int] = set()
    batches = 0
    while True:
        if in_report_window(clock(), config, at):
            stats["paused_for_report"] = 1
            break
        if max_batches is not None and batches >= max_batches:
            break
        done = await _batch(engine, config, profile, versions, clock, stats, seen, batch, notify)
        if done == 0:
            break
        batches += 1
        await sleep(pause)
    result = dict(stats)
    remaining = await pending_count(engine)
    result["remaining"] = remaining
    if stats["recalculated"] or stats["failed"]:
        log.info("recalc_done", extra={"stats": result})
    if remaining == 0 and notify is not None:
        await _notify_done_once(engine, versions, clock, notify)
    return result


async def _notify_done_once(engine, versions, clock, notify) -> None:
    """Одно сообщение о завершении на пару версий: итог берётся из базы, а не из последнего запуска."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    marker = f"{versions.profile_id}:{versions.scoring_id}"
    async with engine.begin() as conn:
        done = (await conn.execute(select(app_state.c.value).where(app_state.c.key == DONE_KEY)
                                   .with_for_update())).scalar()
        if done == marker:
            return
        recalculated = (await conn.execute(select(func.count()).select_from(scores).where(
            scores.c.state == "outdated", scores.c.superseded_by.in_(
                select(scores.c.id).where(scores.c.profile_version_id == versions.profile_id,
                                          scores.c.scoring_version_id == versions.scoring_id))))).scalar_one()
        failed = (await conn.execute(select(func.count()).select_from(scores)
                                     .where(scores.c.state == "recalc_failed"))).scalar_one()
        await conn.execute(pg_insert(app_state).values(key=DONE_KEY, value=marker, updated_at=clock())
                           .on_conflict_do_update(index_elements=[app_state.c.key],
                                                  set_={"value": marker, "updated_at": clock()}))
    if recalculated == 0 and failed == 0:
        return                      # пересчитывать было нечего (первая версия или только новые оценки)
    changed = await _category_changes(engine, versions)
    await notify(f"recalc_done:{marker}",
                 f"Пересчёт оценок завершён: пересчитано {recalculated}, изменили категорию {changed}, "
                 f"ошибок {failed}.")


async def _category_changes(engine, versions) -> int:
    new = scores.alias("new_score")
    async with engine.connect() as conn:
        return (await conn.execute(select(func.count()).select_from(scores).join(new, new.c.id == scores.c.superseded_by)
                                   .where(new.c.profile_version_id == versions.profile_id,
                                          new.c.scoring_version_id == versions.scoring_id,
                                          (scores.c.category != new.c.category)
                                          | (scores.c.gate_result != new.c.gate_result)))).scalar_one()


async def _batch(engine, config, profile, versions, clock, stats, seen, batch, notify) -> int:
    alerts: list[tuple[int, str]] = []
    async with engine.begin() as conn:
        query = (select(scores, vacancies.c.first_seen_at.label("_first_seen"))
                 .join(vacancies, vacancies.c.id == scores.c.vacancy_id)
                 .where(scores.c.state.in_(("pending_recalc", "recalc_failed")))
                 .order_by(vacancies.c.first_seen_at.desc(), scores.c.id).limit(batch)
                 .with_for_update(of=scores, skip_locked=True))
        if seen:
            query = query.where(scores.c.id.notin_(seen))
        rows = (await conn.execute(query)).mappings().all()
        now = clock()
        for row in rows:
            old = dict(row)
            seen.add(old["id"])
            vacancy = dict((await conn.execute(select(vacancies).where(vacancies.c.id == old["vacancy_id"])))
                           .mappings().one())
            try:
                async with conn.begin_nested():
                    extraction = None
                    if old["kind"] == "full":
                        extraction = (await conn.execute(select(extractions).where(
                            extractions.c.id == old["extraction_id"]))).mappings().first() \
                            if old["extraction_id"] else None
                        extraction = dict(extraction) if extraction else None
                        if extraction is None or not vacancy.get("description_text"):
                            # Текст или извлечение очищены сроком хранения: такие оценки не пересчитываются
                            # (DATABASE_SCHEMA.md, раздел 12) и остаются по прежним версиям.
                            await conn.execute(update(scores).where(scores.c.id == old["id"]).values(
                                state="current", error_text=None))
                            stats["skipped_no_text"] += 1
                            continue
                    result = compute(vacancy, extraction, old["kind"], profile, config, now)
                    await store_score(conn, vacancy["id"], result, versions, old["extraction_id"], now)
                    # Статус вакансии ведёт полная оценка; предварительная — только пока полной нет.
                    # Условие на статус в самом UPDATE: «показана» или ушедшая на повторную обработку
                    # вакансия статус не меняет, даже если он сменился после чтения.
                    if old["kind"] == "full" or not await has_active_full(conn, vacancy["id"]):
                        await conn.execute(update(vacancies).where(
                            vacancies.c.id == vacancy["id"], vacancies.c.status.in_(("scored", "rejected_gate"))
                        ).values(status="scored" if result.gate_result == "passed" else "rejected_gate",
                                 status_reason=gate_reason(result), updated_at=now))
            except Exception as exc:  # одна оценка не останавливает пересчёт остальных
                count = _failures(old["error_text"]) + 1
                await conn.execute(update(scores).where(scores.c.id == old["id"]).values(
                    state="recalc_failed", error_text=f"[{count}] {type(exc).__name__}: {exc}"[:500]))
                stats["failed"] += 1
                log.warning("recalc_failed", extra={"score_id": old["id"], "vacancy_id": vacancy["id"],
                                                    "error": f"{type(exc).__name__}: {exc}"[:300]})
                if count == FAILURES_TO_ALERT:
                    alerts.append((vacancy["id"], f"{type(exc).__name__}: {exc}"[:300]))
                continue
            stats["recalculated"] += 1
            if result.category != old["category"] or result.gate_result != old["gate_result"]:
                stats["category_changed"] += 1
    if notify is not None:
        for vacancy_id, error in alerts:
            await notify(f"recalc_failed:{vacancy_id}",
                         f"Пересчёт оценки вакансии {vacancy_id} не удался {FAILURES_TO_ALERT} раза подряд: {error}")
    return len(rows)
