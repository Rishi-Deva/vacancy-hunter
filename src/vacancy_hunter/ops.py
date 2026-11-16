"""Эксплуатационные задачи: резервная копия с отправкой владельцу и ежедневные задачи по расписанию.

Резервная копия (ARCHITECTURE.md, раздел 15): ежедневный дамп в томе `backups`, ротация
7 ежедневных и 4 еженедельных, файл уходит владельцу в Telegram через `outbox`
(вид `backup`). Если файл больше предела Bot API (50 МБ), приходит сообщение, где копия лежит.
"""

from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.config import DatabaseSettings
from hunter_core.notify import enqueue
from hunter_core.scheduler.jobs import local_now
from hunter_core.storage.backup import make_backup, rotate
from hunter_core.storage.tables import job_runs
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig

BACKUP_DIR = Path(os.environ.get("HUNTER_BACKUP_DIR", "/app/backups"))
TELEGRAM_FILE_LIMIT = 49 * 1024 * 1024


async def run_backup(engine: AsyncEngine, config: AppConfig, settings: DatabaseSettings, *,
                     owner_chat_id: int | None, directory: Path = BACKUP_DIR, clock: Clock = utcnow,
                     wake=None) -> dict:
    backup = await make_backup(settings, directory, clock())
    removed = rotate(directory, config.retention.backups_daily, config.retention.backups_weekly)
    size = backup.path.stat().st_size
    local = local_now(clock, config.schedule.timezone)
    if owner_chat_id is not None:
        caption = f"Резервная копия базы за {local:%d.%m.%Y %H:%M}, {size / 1024 / 1024:.1f} МБ"
        if size <= TELEGRAM_FILE_LIMIT:
            payload = {"document_path": str(backup.path), "caption": caption}
        else:
            payload = {"text": f"{caption}: файл больше предела Telegram, он сохранён на сервере "
                               f"в томе backups ({backup.path.name})."}
        async with engine.begin() as conn:
            await enqueue(conn, dedup_key=f"backup:{backup.path.name}", chat_id=owner_chat_id, kind="backup",
                          payload=payload, clock=clock)
        if wake is not None:
            wake()
    return {"file": backup.path.name, "bytes": size, "removed": len(removed)}


def done_today(engine: AsyncEngine, name: str, clock: Clock, timezone: str):
    """Для догоняющего запуска: была ли сегодня (по Тбилиси) успешная задача."""

    async def check() -> bool:
        local = local_now(clock, timezone)
        midnight = datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)
        async with engine.connect() as conn:
            return (await conn.execute(select(job_runs.c.id).where(
                job_runs.c.job_name == name, job_runs.c.status == "success",
                job_runs.c.started_at >= midnight).limit(1))).first() is not None

    return check


__all__ = ["BACKUP_DIR", "done_today", "run_backup"]
