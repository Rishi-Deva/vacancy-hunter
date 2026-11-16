"""Резервные копии базы (ARCHITECTURE.md, раздел 15; DATABASE_SCHEMA.md, раздел 12).

- ежедневный дамп `pg_dump` в формате custom (сжатый, восстанавливается `pg_restore`);
- пароль передаётся переменной окружения процесса, а не аргументом: его не видно в списке процессов
  и он не попадает в журнал;
- файл сначала пишется во временный и переименовывается только после успешного завершения:
  обрыв не оставляет «копию», которую нельзя восстановить;
- хранится 7 последних ежедневных копий и по одной последней копии за каждую из 4 предыдущих недель.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hunter_core.config import DatabaseSettings

log = logging.getLogger("hunter_core.backup")

PREFIX = "backup-"
SUFFIX = ".dump"
TIMEOUT_SECONDS = 600


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupFile:
    path: Path
    created: datetime


def _env(settings: DatabaseSettings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(PGHOST=settings.host, PGPORT=str(settings.port), PGUSER=settings.user,
               PGPASSWORD=settings.password, PGDATABASE=settings.name)
    return env


async def _run(args: list[str], settings: DatabaseSettings, what: str) -> None:
    proc = await asyncio.create_subprocess_exec(*args, env=_env(settings), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackupError(f"{what}: превышено время {TIMEOUT_SECONDS} с") from None
    if proc.returncode != 0:
        message = err.decode("utf-8", "replace").strip()
        if settings.password:
            message = message.replace(settings.password, "***")
        message = message[-500:]
        raise BackupError(f"{what} завершился с кодом {proc.returncode}: {message}")


async def make_backup(settings: DatabaseSettings, directory: Path, now: datetime) -> BackupFile:
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{PREFIX}{now:%Y%m%d-%H%M%S}{SUFFIX}"
    partial = final.with_suffix(".partial")
    try:
        await _run(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(partial)],
                   settings, "pg_dump")
        partial.rename(final)
    finally:
        partial.unlink(missing_ok=True)
    log.info("backup_created", extra={"file": final.name, "bytes": final.stat().st_size})
    return BackupFile(final, now)


async def restore_backup(settings: DatabaseSettings, path: Path) -> None:
    """Развернуть копию в базу из settings: существующие объекты удаляются и создаются заново."""
    await _run(["pg_restore", "--no-owner", "--no-privileges", "--clean", "--if-exists", "--exit-on-error",
                "--single-transaction", "--dbname", settings.name, str(path)], settings, "pg_restore")


def list_backups(directory: Path) -> list[BackupFile]:
    result = []
    for path in directory.glob(f"{PREFIX}*{SUFFIX}"):
        try:
            created = datetime.strptime(path.name[len(PREFIX):-len(SUFFIX)], "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        result.append(BackupFile(path, created))
    return sorted(result, key=lambda b: b.created, reverse=True)


def to_keep(backups: list[BackupFile], daily: int, weekly: int) -> set[Path]:
    """Самая свежая копия каждой из `daily` последних дат плюс самая свежая копия каждой из `weekly`
    предыдущих недель. Несколько ручных копий за день не вытесняют копии прошлых дней."""
    ordered = sorted(backups, key=lambda b: b.created, reverse=True)
    keep: set[Path] = set()
    days: list = []
    for b in ordered:
        day = b.created.date()
        if day in days:
            continue
        if len(days) >= daily:
            break
        days.append(day)
        keep.add(b.path)
    covered_weeks = {d.isocalendar()[:2] for d in days}
    weeks: list = []
    oldest_day = days[-1] if days else None
    for b in ordered:
        if oldest_day is not None and b.created.date() >= oldest_day:
            continue
        week = b.created.isocalendar()[:2]
        if week in covered_weeks or week in weeks:
            continue
        if len(weeks) >= weekly:
            break
        weeks.append(week)
        keep.add(b.path)
    return keep


def rotate(directory: Path, daily: int, weekly: int) -> list[Path]:
    backups = list_backups(directory)
    keep = to_keep(backups, daily, weekly)
    removed = []
    for b in backups:
        if b.path not in keep:
            b.path.unlink(missing_ok=True)
            removed.append(b.path)
    # Остатки оборванных копий; свежий .partial может принадлежать копии, которая пишется сейчас.
    cutoff = time.time() - 2 * TIMEOUT_SECONDS
    for leftover in directory.glob(f"{PREFIX}*.partial"):
        try:
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
    return removed
