"""Этап 1–2 конвейера: сбор и нормализация (ARCHITECTURE.md, раздел 4).

Новые записи источника сохраняются как вакансии со статусом `new` и связью в `vacancy_sources`
в той же транзакции, что и сырые записи и сдвиг курсора. Дедупликация и правила — шаг A4.
Запись, не прошедшая нормализацию, остаётся в `raw_items`, вакансия не создаётся.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.sources import RawItem, SourceRunner
from hunter_core.sources.runner import ControlPolicy
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.pipeline.normalize import NORMALIZERS, NormalizeError
from vacancy_hunter.sources import build_adapters
from vacancy_hunter.sources.factory import build_http
from vacancy_hunter.storage.tables import vacancies, vacancy_sources

log = logging.getLogger("vacancy_hunter.collect")


def make_store(adapter_types: dict[str, str], clock: Clock):
    async def store(conn: AsyncConnection, source_code: str, mode: str,
                    items: list[tuple[int, RawItem]]) -> tuple[int, int]:
        normalizer = NORMALIZERS[adapter_types[source_code]]
        now: datetime = clock()
        created = failed = 0
        for raw_id, item in items:
            try:
                vacancy = normalizer(item.payload, source_code=source_code, quiet=(mode == "quiet"), now=now,
                                     external_id=item.external_id, url=item.url, published_at=item.published_at)
            except (NormalizeError, KeyError, TypeError, ValueError) as exc:
                failed += 1
                log.warning("normalize_failed", extra={"source": source_code, "external_id": item.external_id,
                                                       "error": str(exc)[:200]})
                continue
            exists = (await conn.execute(
                vacancy_sources.select().where(vacancy_sources.c.source_code == source_code,
                                               vacancy_sources.c.external_id == item.external_id)
            )).first()
            if exists:
                # Запись уже известна по связи (например, сырая запись была очищена): дубля не создаём.
                continue
            vacancy_id = (await conn.execute(
                vacancies.insert().values(**vacancy.values).returning(vacancies.c.id)
            )).scalar_one()
            await conn.execute(vacancy_sources.insert().values(
                vacancy_id=vacancy_id, source_code=source_code, external_id=item.external_id, url=item.url,
                raw_item_id=raw_id, first_seen_at=now,
            ))
            created += 1
        return created, failed

    return store


def control_policy(config: AppConfig) -> ControlPolicy:
    from datetime import timedelta

    d = config.sources.defaults
    return ControlPolicy(failures_to_degrade=d.failures_to_degrade, empty_runs_to_alert=d.empty_runs_to_alert,
                         interval=timedelta(hours=d.interval_hours),
                         degraded_interval=timedelta(hours=d.degraded_interval_hours),
                         max_age=timedelta(days=d.max_age_days))


async def collect(engine: AsyncEngine, config: AppConfig, *, alert=None, clock: Clock = utcnow,
                  only: str | None = None, force: bool = False, http=None) -> dict:
    """Один проход сбора по всем включённым источникам (или по одному)."""
    # Источники, которые не опрашиваются (waiting, disabled), тоже видны в /status с режимом из конфигурации.
    from hunter_core.sources import ensure_state

    async with engine.begin() as conn:
        for entry in config.sources.sources:
            if entry.mode not in ("active", "quiet"):
                await ensure_state(conn, entry.code, entry.kind, entry.mode, clock())
    adapters = [(a, m) for a, m in build_adapters(config.sources) if only is None or a.code == only]
    adapter_types = {e.code: e.adapter for e in config.sources.sources}
    runner = SourceRunner(engine, make_store(adapter_types, clock), alert=alert, policy=control_policy(config),
                          clock=clock)
    own_http = http is None
    http = http or build_http(adapters, config.sources)
    try:
        outcomes = await runner.run_all(adapters, lambda _a: http, force=force)
    finally:
        if own_http:
            await http.aclose()
    stats = {"sources": len(outcomes), "success": 0, "failed": 0, "blocked": 0, "skipped": 0, "new": 0}
    for o in outcomes:
        stats[o.status] += 1
        stats["new"] += o.items_new
    return stats
