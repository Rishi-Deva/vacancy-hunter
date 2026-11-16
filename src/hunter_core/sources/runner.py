"""Запуск сбора и оперативный контроль источников (ARCHITECTURE.md, раздел 6; SOURCES.md, раздел 8).

Один запуск по одному источнику:
1. строка `fetch_runs` со статусом `running`;
2. адаптер получает записи по курсору;
3. в одной транзакции: сохранение `raw_items` (повтор той же записи строку не создаёт),
   передача новых записей продукту (`store`), сдвиг курсора, итог запуска, сброс счётчика сбоев.
   Если сохранение не удалось, курсор не сдвигается и интервал будет собран заново;
4. при сбое: курсор не сдвигается, `consecutive_failures` растёт.
   5 сбоев подряд → `degraded` (опрос раз в 12 часов) и одно оповещение;
   явный запрет (403, 451) → режим `disabled`, `health` = `blocked`, оповещение сразу;
   ноль записей на страницах 3 запуска подряд → `degraded` и одно оповещение.
Источники обрабатываются независимо: сбой одного не останавливает остальные.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.http import HttpClient
from hunter_core.sources.base import (
    FetchContext,
    RawItem,
    SourceAdapter,
    SourceBlocked,
    SourceError,
    SourceTemporaryError,
)
from hunter_core.storage.tables import fetch_runs, raw_items, source_state
from hunter_core.timeutil import Clock, utcnow

log = logging.getLogger("hunter_core.sources")

# Продукт сохраняет новые записи в своих таблицах в той же транзакции. Возвращает число
# новых публикаций для счётчика и число записей, не прошедших нормализацию.
StoreFunc = Callable[[AsyncConnection, str, str, list[tuple[int, RawItem]]], Awaitable[tuple[int, int]]]
AlertFunc = Callable[[str, str], Awaitable[Any]]


@dataclass(frozen=True)
class ControlPolicy:
    failures_to_degrade: int = 5
    empty_runs_to_alert: int = 3
    interval: timedelta = timedelta(hours=3)
    degraded_interval: timedelta = timedelta(hours=12)
    max_age: timedelta = timedelta(days=14)
    normalize_fail_alert_share: float = 0.2
    run_timeout: float = 300.0  # предел времени на сбор одного источника, секунды


@dataclass
class RunOutcome:
    source: str
    status: str               # success, failed, blocked, skipped
    items_seen: int = 0
    items_new: int = 0
    error: str | None = None


async def ensure_state(conn: AsyncConnection, code: str, kind: str, mode: str, now) -> None:
    """Создать строку состояния при первом запуске. Режим из конфигурации применяется,
    если источник не отключён системой из-за запрета (такое снимает только владелец)."""
    await conn.execute(
        pg_insert(source_state).values(code=code, kind=kind, mode=mode, mode_reason="owner", health="ok",
                                       created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=[source_state.c.code])
    )
    await conn.execute(
        update(source_state)
        .where(source_state.c.code == code, source_state.c.health != "blocked", source_state.c.mode != mode)
        .values(mode=mode, mode_reason="owner", updated_at=now)
    )


class SourceRunner:
    def __init__(self, engine: AsyncEngine, store: StoreFunc, *, alert: AlertFunc | None = None,
                 policy: ControlPolicy | None = None, clock: Clock = utcnow):
        self.engine = engine
        self.store = store
        self.alert = alert
        self.policy = policy or ControlPolicy()
        self.clock = clock

    async def _state(self, code: str) -> dict:
        async with self.engine.connect() as conn:
            return dict((await conn.execute(select(source_state).where(source_state.c.code == code)))
                        .mappings().one())

    def due(self, state: dict) -> bool:
        if state["mode"] not in ("active", "quiet"):
            return False
        interval = self.policy.degraded_interval if state["health"] == "degraded" else self.policy.interval
        last = state["last_run_at"]
        # небольшой допуск, чтобы запуск по расписанию «каждые 3 часа» не пропускался из-за секунд
        return last is None or self.clock() - last >= interval - timedelta(minutes=5)

    async def run_source(self, adapter: SourceAdapter, http: HttpClient, *, mode: str,
                         force: bool = False) -> RunOutcome:
        now = self.clock()
        async with self.engine.begin() as conn:
            await ensure_state(conn, adapter.code, adapter.kind, mode, now)
        state = await self._state(adapter.code)
        if not force and not self.due(state):
            return RunOutcome(adapter.code, "skipped")
        if state["mode"] not in ("active", "quiet"):
            return RunOutcome(adapter.code, "skipped")

        async with self.engine.begin() as conn:
            run_id = (await conn.execute(
                pg_insert(fetch_runs).values(source_code=adapter.code, started_at=now, status="running",
                                             cursor_before=state["cursor"]).returning(fetch_runs.c.id)
            )).scalar_one()

        ctx = FetchContext(http=http, now=now, max_age=self.policy.max_age)
        try:
            result = await _with_deadline(adapter.fetch(state["cursor"], ctx), self.policy.run_timeout, adapter.code)
            if result.seen_on_pages == 0:
                raise _EmptyPage("на страницах источника нет ни одной записи")
            seen, new, normalize_failed = await self._save(adapter, run_id, result, now)
        except SourceBlocked as exc:
            await self._fail(adapter.code, run_id, exc, blocked=True)
            return RunOutcome(adapter.code, "blocked", error=str(exc))
        except (SourceError, _EmptyPage) as exc:
            await self._fail(adapter.code, run_id, exc, blocked=False, empty=isinstance(exc, _EmptyPage))
            return RunOutcome(adapter.code, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 — непредвиденная ошибка одного источника
            log.exception("source_unexpected_error", extra={"source": adapter.code})
            await self._fail(adapter.code, run_id, exc, blocked=False)
            return RunOutcome(adapter.code, "failed", error=f"{type(exc).__name__}: {exc}")

        if state["health"] == "degraded" and self.alert is not None:
            await self.alert(f"source_recovered:{adapter.code}", f"Источник {adapter.code} снова работает")
        if seen and normalize_failed / seen > self.policy.normalize_fail_alert_share and self.alert is not None:
            await self.alert(f"source_normalize:{adapter.code}",
                             f"Источник {adapter.code}: {normalize_failed} из {seen} записей не разобраны")
        log.info("source_collected", extra={"source": adapter.code, "seen": seen, "new": new,
                                            "normalize_failed": normalize_failed})
        return RunOutcome(adapter.code, "success", items_seen=seen, items_new=new)

    async def _save(self, adapter: SourceAdapter, run_id: int, result, now) -> tuple[int, int, int]:
        async with self.engine.begin() as conn:
            new_items: list[tuple[int, RawItem]] = []
            for item in result.items:
                raw_id = (await conn.execute(
                    pg_insert(raw_items).values(source_code=adapter.code, external_id=item.external_id,
                                                fetch_run_id=run_id, url=item.url, payload=item.payload,
                                                published_at=item.published_at, fetched_at=now)
                    .on_conflict_do_nothing(index_elements=[raw_items.c.source_code, raw_items.c.external_id])
                    .returning(raw_items.c.id)
                )).scalar_one_or_none()
                if raw_id is not None:
                    new_items.append((raw_id, item))
            mode = (await conn.execute(select(source_state.c.mode).where(source_state.c.code == adapter.code)
                                       )).scalar_one()
            posts_new, normalize_failed = await self.store(conn, adapter.code, mode, new_items)
            await conn.execute(update(fetch_runs).where(fetch_runs.c.id == run_id).values(
                status="success", finished_at=self.clock(), items_seen=len(result.items), items_new=len(new_items),
                cursor_after=result.cursor, http_requests=result.http_requests,
            ))
            await conn.execute(update(source_state).where(source_state.c.code == adapter.code).values(
                cursor=result.cursor, last_run_at=now, last_success_at=now, consecutive_failures=0,
                last_error=None, health="ok", posts_total=source_state.c.posts_total + posts_new, updated_at=now,
            ))
        return len(result.items), len(new_items), normalize_failed

    async def _fail(self, code: str, run_id: int, exc: BaseException, *, blocked: bool, empty: bool = False) -> None:
        now = self.clock()
        text = str(exc)[:500] or type(exc).__name__
        async with self.engine.begin() as conn:
            await conn.execute(update(fetch_runs).where(fetch_runs.c.id == run_id).values(
                status="failed", finished_at=now, items_seen=0, items_new=0,
                error_code="blocked" if blocked else ("empty" if empty else type(exc).__name__), error_text=text,
            ))
            row = (await conn.execute(
                update(source_state).where(source_state.c.code == code)
                .values(last_run_at=now, last_error=text, updated_at=now,
                        consecutive_failures=source_state.c.consecutive_failures + 1)
                .returning(source_state.c.consecutive_failures, source_state.c.health)
            )).one()
            failures, health = row
            new_health = health
            if blocked:
                new_health = "blocked"
                await conn.execute(update(source_state).where(source_state.c.code == code).values(
                    mode="disabled", mode_reason="blocked", health="blocked"))
            elif health == "ok" and (failures >= self.policy.failures_to_degrade
                                     or (empty and await self._empty_streak(conn, code))):
                new_health = "degraded"
                await conn.execute(update(source_state).where(source_state.c.code == code).values(health="degraded"))
        log.warning("source_failed", extra={"source": code, "error": text, "failures": failures,
                                            "health": new_health})
        if self.alert is None or new_health == health:
            return
        if new_health == "blocked":
            await self.alert(f"source_blocked:{code}",
                             f"Источник {code} отключён: {text}. Включить снова может только владелец.")
        elif empty and failures < self.policy.failures_to_degrade:
            await self.alert(f"source_empty:{code}",
                             f"Источник {code}: {self.policy.empty_runs_to_alert} запуска подряд без записей. "
                             "Возможно, изменилась разметка страницы.")
        else:
            await self.alert(f"source_degraded:{code}",
                             f"Источник {code}: {failures} сбоев подряд. Опрос раз в 12 часов. Последняя ошибка: {text}")

    async def _empty_streak(self, conn: AsyncConnection, code: str) -> bool:
        last = (await conn.execute(
            select(fetch_runs.c.error_code).where(fetch_runs.c.source_code == code)
            .order_by(fetch_runs.c.id.desc()).limit(self.policy.empty_runs_to_alert)
        )).scalars().all()
        return len(last) == self.policy.empty_runs_to_alert and all(c == "empty" for c in last)

    async def run_all(self, adapters: Sequence[tuple[SourceAdapter, str]], http_for, *,
                      force: bool = False) -> list[RunOutcome]:
        outcomes = []
        for adapter, mode in adapters:
            try:
                outcomes.append(await self.run_source(adapter, http_for(adapter), mode=mode, force=force))
            except Exception as exc:  # noqa: BLE001 — один источник не останавливает остальные
                log.exception("source_run_crashed", extra={"source": adapter.code})
                outcomes.append(RunOutcome(adapter.code, "failed", error=str(exc)))
        return outcomes


class _EmptyPage(Exception):
    pass


async def _with_deadline(coro, seconds: float, code: str):
    """Предел времени, который срабатывает даже для операции, не поддающейся отмене
    (например, зависший поиск адреса в отдельном потоке): такая операция бросается, а не ожидается."""
    task = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    if not done:
        task.cancel()
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # не оставлять «непрочитанную» ошибку
        log.error("source_timeout", extra={"source": code, "seconds": seconds})
        raise SourceTemporaryError(f"{code}: сбор не уложился в {int(seconds)} с")
    return task.result()
