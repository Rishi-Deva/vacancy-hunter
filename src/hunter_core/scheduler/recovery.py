"""Проверка состояния общих таблиц при старте (DATABASE_SCHEMA.md, раздел 13).

Выполняется до запуска планировщика и бота:
- запуски сборщиков `running` старше 15 минут → `abandoned` (курсор не сдвигался);
- задачи `running` с истёкшей арендой → `abandoned`, блокировка снята;
- сообщения в состоянии `sending` → `unconfirmed`: исход неизвестен, повтора нет.

Резервы бюджета (llm_ledger) восстанавливаются на шаге A5 вместе с бюджетом.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import and_, or_, update
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.storage.tables import fetch_runs, job_runs, outbox
from hunter_core.timeutil import Clock, utcnow

log = logging.getLogger("hunter_core.recovery")

STALE_AFTER = timedelta(minutes=15)


async def recover_core_state(engine: AsyncEngine, *, clock: Clock = utcnow) -> dict[str, int]:
    now = clock()
    async with engine.begin() as conn:
        fetch = await conn.execute(
            update(fetch_runs)
            .where(fetch_runs.c.status == "running", fetch_runs.c.started_at < now - STALE_AFTER)
            .values(status="abandoned", finished_at=now, error_text="прервано: процесс остановился")
        )
        jobs = await conn.execute(
            update(job_runs)
            .where(and_(
                job_runs.c.status == "running",
                or_(job_runs.c.lease_until.is_(None), job_runs.c.lease_until < now),
            ))
            .values(status="abandoned", finished_at=now, lease_until=None,
                    error_text="аренда истекла: процесс остановился")
        )
        sending = await conn.execute(
            update(outbox)
            .where(outbox.c.state == "sending")
            .values(state="unconfirmed", error_text="исход отправки неизвестен: процесс остановился")
        )
    result = {"fetch_runs_abandoned": fetch.rowcount, "job_runs_abandoned": jobs.rowcount,
              "outbox_unconfirmed": sending.rowcount}
    log.info("startup_recovery", extra=result)
    return result
