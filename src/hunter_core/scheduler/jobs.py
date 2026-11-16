"""Запуск задач с блокировкой в базе (ARCHITECTURE.md, разделы 11–12; DATABASE_SCHEMA.md, 10.1).

- Перед запуском в `job_runs` вставляется строка `running`. Уникальный частичный индекс
  не даёт вставить вторую такую строку: вторая копия задачи не стартует.
- Пока задача работает, аренда (`lease_until`) продлевается. Аренда, не продлённая
  15 минут, считается брошенной (см. recovery.py).
- Итог задачи — `success` или `failed` с текстом ошибки и статистикой.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.storage.tables import job_runs
from hunter_core.timeutil import Clock, utcnow

log = logging.getLogger("hunter_core.scheduler")

JobFunc = Callable[[], Awaitable[dict[str, Any] | None]]
FailureHook = Callable[[str, BaseException], Awaitable[None]]


class JobAlreadyRunning(Exception):
    pass


class JobRunner:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lease: timedelta = timedelta(minutes=15),
        renew_every: float = 60.0,
        clock: Clock = utcnow,
        on_failure: FailureHook | None = None,
    ):
        self.engine = engine
        self.lease = lease
        self.renew_every = renew_every
        self.clock = clock
        self.on_failure = on_failure

    async def _acquire(self, name: str) -> int:
        now = self.clock()
        try:
            async with self.engine.begin() as conn:
                return (await conn.execute(
                    insert(job_runs)
                    .values(job_name=name, status="running", started_at=now, lease_until=now + self.lease)
                    .returning(job_runs.c.id)
                )).scalar_one()
        except IntegrityError as exc:
            if "uq_job_runs_one_running" in str(exc.orig):
                raise JobAlreadyRunning(name) from None
            raise

    async def _finish(self, run_id: int, status: str, stats: dict | None, error: str | None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                update(job_runs).where(job_runs.c.id == run_id).values(
                    status=status, finished_at=self.clock(), lease_until=None, stats=stats, error_text=error,
                )
            )

    async def _renew_forever(self, run_id: int) -> None:
        while True:
            await asyncio.sleep(self.renew_every)
            try:
                async with asyncio.timeout(30):
                    async with self.engine.begin() as conn:
                        await conn.execute(
                            update(job_runs)
                            .where(job_runs.c.id == run_id, job_runs.c.status == "running")
                            .values(lease_until=self.clock() + self.lease)
                        )
            except Exception:  # noqa: BLE001 — одна неудача не должна останавливать продление
                log.exception("job_lease_renew_failed", extra={"run_id": run_id})

    async def run(self, name: str, func: JobFunc) -> str:
        """Выполнить задачу. Возвращает `success`, `failed` или `skipped` (уже работает)."""
        try:
            run_id = await self._acquire(name)
        except JobAlreadyRunning:
            log.info("job_skipped_already_running", extra={"job": name})
            return "skipped"
        log.info("job_started", extra={"job": name, "run_id": run_id})
        renewer = asyncio.create_task(self._renew_forever(run_id))
        try:
            stats = await func()
        except Exception as exc:  # noqa: BLE001 — ошибка задачи не должна ронять процесс
            renewer.cancel()
            error = f"{type(exc).__name__}: {exc}"[:2000]
            await self._finish(run_id, "failed", None, error)
            log.error("job_failed", extra={"job": name, "run_id": run_id, "error": error})
            if self.on_failure is not None:
                try:
                    await self.on_failure(name, exc)
                except Exception:  # noqa: BLE001
                    log.exception("job_failure_hook_failed", extra={"job": name})
            return "failed"
        renewer.cancel()
        await self._finish(run_id, "success", stats, None)
        log.info("job_finished", extra={"job": name, "run_id": run_id, "stats": stats})
        return "success"


def catchup_due(now_local: datetime, run_at: time, until: time, already_done: bool) -> bool:
    """Нужно ли догнать пропущенный ежедневный запуск сразу после старта (ARCHITECTURE.md, раздел 12).

    Да, если сегодня запуска ещё не было и сейчас между временем запуска и пределом догона
    (по умолчанию 07:00–14:00 по местному времени).
    """
    if already_done:
        return False
    current = now_local.timetz().replace(tzinfo=None)
    return run_at <= current < until


def local_now(clock: Clock, tz: str) -> datetime:
    return clock().astimezone(ZoneInfo(tz))
