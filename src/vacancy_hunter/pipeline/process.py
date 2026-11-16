"""Задача обработки (ARCHITECTURE.md, раздел 11): дедупликация и правила до LLM (A4),
LLM-извлечение (A5), фильтры и скоринг (A6).

Записи обрабатываются пачками в отдельных транзакциях. Каждый этап идемпотентен:
повторный запуск не меняет уже обработанные записи.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.pipeline.dedup import find_original, mark_duplicate
from vacancy_hunter.pipeline.normalize import norm_title
from vacancy_hunter.pipeline.prefilter import PRELIMINARY_PENDING, decide
from vacancy_hunter.storage.tables import vacancies

log = logging.getLogger("vacancy_hunter.process")

BATCH = 200


async def dedup_and_prefilter(engine: AsyncEngine, config: AppConfig, *, clock: Clock = utcnow) -> dict:
    stats: Counter = Counter()
    # Несколько проходов: оригинал, получивший описание от дубля, снова становится `new`
    # и обрабатывается в следующем проходе той же задачи. Каждый проход не короче одной записи.
    for _ in range(5):
        handled = await _pass(engine, config, clock, stats)
        if handled == 0:
            break
    result = dict(stats)
    if result:
        log.info("dedup_prefilter_done", extra={"stats": result})
    return result


async def _pass(engine: AsyncEngine, config: AppConfig, clock: Clock, stats: Counter) -> int:
    handled = 0
    last_id = 0
    while True:
        async with engine.begin() as conn:
            rows = (await conn.execute(
                select(vacancies)
                .where(vacancies.c.status == "new", vacancies.c.id > last_id,
                       or_(vacancies.c.status_reason.is_(None), vacancies.c.status_reason != PRELIMINARY_PENDING))
                .order_by(vacancies.c.id).limit(BATCH).with_for_update(skip_locked=True)
            )).mappings().all()
            if not rows:
                break
            now = clock()
            for row in rows:
                vacancy = dict(row)
                last_id = vacancy["id"]
                handled += 1
                # Нормализованное название пересчитывается по текущим правилам нормализации.
                vacancy["title_norm"] = norm_title(vacancy["title"])
                await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"])
                                   .values(title_norm=vacancy["title_norm"]))
                match = await find_original(conn, vacancy, now)
                if match is not None:
                    await mark_duplicate(conn, vacancy, match, now)
                    stats["duplicate"] += 1
                    continue
                decision = decide(vacancy, config.scoring, now)
                await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
                    status=decision.status, status_reason=decision.reason, updated_at=now))
                key = decision.status if decision.status != "rejected_rules" else f"rejected:{decision.reason}"
                stats[key] += 1
    return handled


async def process(engine: AsyncEngine, config: AppConfig, *, clock: Clock = utcnow, gateway=None) -> dict:
    """Одна задача обработки: дедупликация и правила (A4), LLM-извлечение (A5), скоринг (A6).

    Без шлюза (ключ API не задан) извлечение пропускается: записи ждут в `pending_llm`,
    а скоринг выполняется для уже извлечённых и для оценок по заголовку.
    """
    from vacancy_hunter.pipeline.score import score_pending

    result = {"dedup_prefilter": await dedup_and_prefilter(engine, config, clock=clock)}
    if gateway is not None:
        from vacancy_hunter.pipeline.extract import Extractor

        result["recovered_reserves"] = await gateway.budget.recover_stale()
        result["extraction"] = await Extractor(engine, gateway, config, clock=clock).run()
    result["scoring"] = await score_pending(engine, config, clock=clock)
    return result
