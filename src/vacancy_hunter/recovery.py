"""Проверка состояния при старте: общие таблицы и таблицы вакансий (DATABASE_SCHEMA.md, раздел 13)."""

from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.scheduler import recover_core_state
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.storage.tables import vacancies

log = logging.getLogger("vacancy_hunter.recovery")


async def recover_state(engine: AsyncEngine, *, clock: Clock = utcnow) -> dict[str, int]:
    result = await recover_core_state(engine, clock=clock)
    async with engine.begin() as conn:
        leases = await conn.execute(
            update(vacancies).where(vacancies.c.lease_until < clock()).values(lease_until=None)
        )
    result["vacancy_leases_released"] = leases.rowcount
    log.info("startup_recovery_vacancies", extra={"vacancy_leases_released": leases.rowcount})
    return result
