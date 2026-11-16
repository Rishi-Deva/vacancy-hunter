"""Обёртка APScheduler 3.x (решение D3). Постоянное хранилище заданий не используется:
расписание задаётся в коде и конфигурации, состояние — в `job_runs`."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger

from hunter_core.scheduler.jobs import JobFunc, JobRunner

log = logging.getLogger("hunter_core.scheduler")


class Scheduler:
    def __init__(self, runner: JobRunner | None, timezone: str):
        self.runner = runner
        self._aps = AsyncIOScheduler(timezone=timezone)

    def add_locked(self, name: str, func: JobFunc, trigger: BaseTrigger) -> None:
        """Задача с блокировкой в `job_runs`: вторая копия не стартует."""
        assert self.runner is not None
        runner = self.runner

        async def wrapped() -> None:
            await runner.run(name, func)

        self._add(name, wrapped, trigger)

    def add_simple(self, name: str, func: Callable[[], Awaitable[None]], trigger: BaseTrigger) -> None:
        """Лёгкая служебная задача без записи в `job_runs` (например, пульс)."""

        async def wrapped() -> None:
            try:
                await func()
            except Exception:  # noqa: BLE001
                log.exception("task_failed", extra={"task": name})

        self._add(name, wrapped, trigger)

    def _add(self, name: str, func, trigger: BaseTrigger) -> None:
        self._aps.add_job(func, trigger, id=name, name=name, max_instances=1, coalesce=True,
                          misfire_grace_time=300, replace_existing=True)

    def start(self) -> None:
        self._aps.start()
        log.info("scheduler_started", extra={"jobs": [j.id for j in self._aps.get_jobs()]})

    def shutdown(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)

    @property
    def job_ids(self) -> list[str]:
        return [j.id for j in self._aps.get_jobs()]
