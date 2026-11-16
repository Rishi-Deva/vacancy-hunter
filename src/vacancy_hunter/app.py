"""Процесс приложения: один asyncio-процесс (ARCHITECTURE.md, раздел 3).

Порядок запуска (TEST_PLAN BOT-3): проверка состояния → отправщик outbox → планировщик
(и догоняющие запуски) → бот. Бот и фоновые задачи работают в одном цикле событий,
ни одна задача не блокирует обработку команд.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import time

from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.notify import Alerts, MessageSender, OutboxSender
from hunter_core.observability.heartbeat import Heartbeat
from hunter_core.scheduler import JobRunner, Scheduler, catchup_due
from hunter_core.scheduler.jobs import JobFunc, local_now
from hunter_core.storage.tables import app_state
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.bot import BotContext, build_dispatcher
from vacancy_hunter.config import AppConfig
from vacancy_hunter.recovery import recover_state

log = logging.getLogger("vacancy_hunter.app")


@dataclass
class DailyJob:
    """Ежедневная задача с догоняющим запуском после перезапуска (например, отчёт — шаг A7)."""

    name: str
    func: JobFunc
    run_at: time
    already_done: Callable[[], Awaitable[bool]]


@dataclass
class Application:
    config: AppConfig
    engine: AsyncEngine
    sender: MessageSender | None
    owner_id: int | None
    heartbeat: Heartbeat
    bot_token: str | None = None
    clock: Clock = utcnow
    daily_jobs: list[DailyJob] = field(default_factory=list)
    startup_jobs: list[tuple[str, JobFunc]] = field(default_factory=list)  # сбор и обработка — шаги A3, A4
    events: list[str] = field(default_factory=list)  # порядок запуска, для проверки в тестах

    def __post_init__(self) -> None:
        tz = self.config.schedule.timezone
        self.outbox = OutboxSender(self.engine, self.sender, clock=self.clock,
                                   interval=self.config.schedule.outbox_every_seconds) if self.sender else None
        wake = self.outbox.wake if self.outbox else None
        self.alerts = Alerts(self.engine, self.owner_id, timezone=tz, clock=self.clock, wake=wake)
        self.runner = JobRunner(self.engine, clock=self.clock, on_failure=self.alerts.job_failed)
        self.scheduler = Scheduler(self.runner, tz)
        self.bot_context = BotContext(
            engine=self.engine, owner_id=self.owner_id, timezone=tz,
            report_time=self.config.schedule.report_time, wake_sender=wake or (lambda: None), clock=self.clock,
            config=self.config,
        )
        self.dispatcher = build_dispatcher(self.bot_context)
        self.gateway = None   # LLM-шлюз, если задан ключ API (шаг A5)
        self._tasks: list[asyncio.Task] = []
        self._outbox_task: asyncio.Task | None = None

    async def beat(self) -> None:
        if await self.heartbeat.beat():
            async with self.engine.begin() as conn:
                stamp = local_now(self.clock, self.config.schedule.timezone).strftime("%d.%m %H:%M")
                await conn.execute(
                    pg_insert(app_state).values(key="heartbeat_last_ok", value=stamp, updated_at=self.clock())
                    .on_conflict_do_update(index_elements=[app_state.c.key],
                                           set_={"value": stamp, "updated_at": self.clock()})
                )

    async def run_catchups(self) -> list[str]:
        """Пропущенные запуски: ежедневные — если окно не прошло; сбор и обработка — всегда."""
        started = []
        schedule = self.config.schedule
        now_local = local_now(self.clock, schedule.timezone)
        until = time.fromisoformat(schedule.report_catchup_until)
        for job in self.daily_jobs:
            if catchup_due(now_local, job.run_at, until, await job.already_done()):
                started.append(job.name)
                self._tasks.append(asyncio.create_task(self.runner.run(job.name, job.func)))
        for name, func in self.startup_jobs:
            started.append(name)
            self._tasks.append(asyncio.create_task(self.runner.run(name, func)))
        if started:
            log.info("catchup_started", extra={"jobs": started})
        return started

    async def start(self) -> None:
        await recover_state(self.engine, clock=self.clock)
        self.events.append("recovery")
        if self.outbox is not None:
            self._outbox_task = asyncio.create_task(self.outbox.run_forever())
            self.events.append("outbox")
        else:
            log.warning("telegram_disabled", extra={"reason": "TELEGRAM_BOT_TOKEN не задан: бот и отправка отключены"})
        if self.heartbeat.enabled:
            self.scheduler.add_simple("heartbeat", self.beat,
                                      IntervalTrigger(minutes=self.config.schedule.heartbeat_every_minutes))
            self._tasks.append(asyncio.create_task(self.beat()))
        else:
            log.warning("heartbeat_disabled", extra={"reason": "HEARTBEAT_URL не задан"})
        self.scheduler.start()
        self.events.append("scheduler")
        await self.run_catchups()
        if self.bot_token:
            self._tasks.append(asyncio.create_task(self._poll()))
            self.events.append("bot")
        log.info("app_started", extra={"bot": bool(self.bot_token), "owner_set": self.owner_id is not None,
                                       "heartbeat": self.heartbeat.enabled})

    async def _poll(self) -> None:
        from aiogram import Bot

        bot = Bot(self.bot_token)
        try:
            await self.dispatcher.start_polling(bot, handle_signals=False, allowed_updates=["message",
                                                                                            "callback_query"])
        finally:
            await bot.session.close()

    async def stop(self) -> None:
        self.scheduler.shutdown()
        if self._outbox_task is not None:
            # Дать отправщику закончить текущее сообщение: иначе оно останется «не подтверждено».
            self.outbox.stop()
            try:
                await asyncio.wait_for(self._outbox_task, timeout=15)
            except TimeoutError:
                log.warning("outbox_stop_timeout")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.heartbeat.aclose()
        log.info("app_stopped")


async def report_done_today(engine: AsyncEngine, clock: Clock, timezone: str) -> bool:
    """Есть ли ежедневный отчёт за сегодняшнюю дату (по Asia/Tbilisi)."""
    from vacancy_hunter.storage.tables import reports

    today = local_now(clock, timezone).date()
    async with engine.connect() as conn:
        return (await conn.execute(
            select(reports.c.id).where(reports.c.kind == "daily", reports.c.report_date == today)
        )).first() is not None
