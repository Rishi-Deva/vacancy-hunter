"""Бюджет LLM: резервирование до вызова (ARCHITECTURE.md, раздел 8; DATABASE_SCHEMA.md, раздел 9).

Резерв — одна транзакция с блокировкой строки месяца в `llm_budget`:
1. строка месяца блокируется (параллельные запросы ждут и видят уже обновлённые суммы);
2. проверяются условия: общий лимит; для извлечения и проверки каналов — остаток за вычетом
   резерва под письма и суточный лимит (по журналу за сутки UTC); для проверки каналов — её лимит;
3. если хотя бы одно не выполнено — ничего не меняется, вызов не выполняется;
4. иначе резерв растёт, в журнал пишется строка `reserved`.
Завершение — вторая транзакция: `settled` (факт), `released` (запрос не ушёл),
`assumed_spent` (исход неизвестен — расход по верхней границе).
Независимо от этого база сама отклоняет строку, где расход плюс резерв больше лимита.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.llm.pricing import Usage
from hunter_core.storage.tables import llm_budget, llm_ledger
from hunter_core.timeutil import Clock, utcnow

log = logging.getLogger("hunter_core.llm.budget")

EXTRACTION_PURPOSES = ("extraction", "quiet_check")
STALE_RESERVE = timedelta(minutes=15)


@dataclass(frozen=True)
class BudgetLimits:
    monthly: Decimal
    letters_reserve: Decimal
    daily_extraction: Decimal
    quiet: Decimal
    warn1: Decimal
    warn2: Decimal


class BudgetExhausted(Exception):
    """Вызов не выполняется: лимит исчерпан. `scope` — какой: monthly, extraction, daily, quiet."""

    def __init__(self, scope: str, message: str):
        super().__init__(message)
        self.scope = scope


AlertFunc = Callable[[str, str], Awaitable[Any]]


def month_start(moment: datetime) -> date:
    return moment.date().replace(day=1)


class Budget:
    def __init__(self, engine: AsyncEngine, limits: BudgetLimits, *, clock: Clock = utcnow,
                 alert: AlertFunc | None = None):
        self.engine = engine
        self.limits = limits
        self.clock = clock
        self.alert = alert

    async def _lock_month(self, conn: AsyncConnection, now: datetime) -> dict:
        month = month_start(now)
        await conn.execute(
            pg_insert(llm_budget).values(
                month=month, limit_usd=self.limits.monthly, letters_reserve_usd=self.limits.letters_reserve,
                daily_extraction_limit_usd=self.limits.daily_extraction, quiet_limit_usd=self.limits.quiet,
                warn1_usd=self.limits.warn1, warn2_usd=self.limits.warn2, updated_at=now,
            ).on_conflict_do_nothing(index_elements=[llm_budget.c.month])
        )
        return dict((await conn.execute(
            select(llm_budget).where(llm_budget.c.month == month).with_for_update()
        )).mappings().one())

    async def _today_extraction(self, conn: AsyncConnection, now: datetime) -> Decimal:
        day = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
        value = (await conn.execute(
            select(func.coalesce(func.sum(func.coalesce(llm_ledger.c.actual_usd, llm_ledger.c.reserved_usd)), 0))
            .where(llm_ledger.c.purpose.in_(EXTRACTION_PURPOSES), llm_ledger.c.started_at >= day,
                   llm_ledger.c.state != "released")
        )).scalar_one()
        return Decimal(value)

    async def _quiet_reserved(self, conn: AsyncConnection, month: date) -> Decimal:
        value = (await conn.execute(
            select(func.coalesce(func.sum(llm_ledger.c.reserved_usd), 0))
            .where(llm_ledger.c.month == month, llm_ledger.c.purpose == "quiet_check", llm_ledger.c.state == "reserved")
        )).scalar_one()
        return Decimal(value)

    async def reserve(self, *, purpose: str, model: str, amount: Decimal, attempt_no: int,
                      part_no: int | None = None, extra: dict | None = None) -> int:
        """Зарезервировать сумму. Возвращает номер строки журнала или поднимает BudgetExhausted."""
        now = self.clock()
        async with self.engine.begin() as conn:
            row = await self._lock_month(conn, now)
            spent, reserved, limit = row["spent_usd"], row["reserved_usd"], row["limit_usd"]
            if spent + reserved + amount > limit:
                raise BudgetExhausted("monthly", "исчерпан месячный лимит LLM")
            if purpose in EXTRACTION_PURPOSES:
                if spent + reserved + amount > limit - row["letters_reserve_usd"]:
                    raise BudgetExhausted("extraction", "исчерпана доступная извлечению часть месячного лимита")
                if await self._today_extraction(conn, now) + amount > row["daily_extraction_limit_usd"]:
                    raise BudgetExhausted("daily", "исчерпан суточный лимит извлечения")
            if purpose == "quiet_check":
                used = row["quiet_spent_usd"] + await self._quiet_reserved(conn, row["month"])
                if used + amount > row["quiet_limit_usd"]:
                    raise BudgetExhausted("quiet", "исчерпан лимит на проверку каналов")
            await conn.execute(update(llm_budget).where(llm_budget.c.month == row["month"])
                               .values(reserved_usd=reserved + amount, updated_at=now))
            ledger_id = (await conn.execute(insert(llm_ledger).values(
                month=row["month"], purpose=purpose, model=model, attempt_no=attempt_no, part_no=part_no,
                state="reserved", reserved_usd=amount, started_at=now, **(extra or {}),
            ).returning(llm_ledger.c.id))).scalar_one()
        return ledger_id

    async def _close(self, ledger_id: int, *, state: str, spent: Decimal, usage: Usage | None = None,
                     stop_reason: str | None = None, error: str | None = None) -> None:
        now = self.clock()
        async with self.engine.begin() as conn:
            entry = dict((await conn.execute(select(llm_ledger).where(llm_ledger.c.id == ledger_id))).mappings().one())
            if entry["state"] != "reserved":
                return  # уже закрыт (например, восстановлением)
            row = dict((await conn.execute(
                select(llm_budget).where(llm_budget.c.month == entry["month"]).with_for_update()
            )).mappings().one())
            # Фактическая стоимость не может превысить резерв: верхняя граница считается с запасом.
            spent = min(spent, entry["reserved_usd"])
            values = {"reserved_usd": row["reserved_usd"] - entry["reserved_usd"],
                      "spent_usd": row["spent_usd"] + spent, "updated_at": now}
            if entry["purpose"] == "quiet_check":
                values["quiet_spent_usd"] = row["quiet_spent_usd"] + spent
            await conn.execute(update(llm_budget).where(llm_budget.c.month == entry["month"]).values(**values))
            ledger_values: dict[str, Any] = {"state": state, "actual_usd": spent, "finished_at": now,
                                             "stop_reason": stop_reason, "error_text": (error or None) and error[:1000]}
            if usage is not None:
                ledger_values.update(tokens_in=usage.input_tokens, tokens_cache_write=usage.cache_creation_input_tokens,
                                     tokens_cache_read=usage.cache_read_input_tokens, tokens_out=usage.output_tokens)
            await conn.execute(update(llm_ledger).where(llm_ledger.c.id == ledger_id).values(**ledger_values))
            warn = None
            new_spent = values["spent_usd"]
            if new_spent >= row["warn2_usd"] and not row["warn2_sent"]:
                await conn.execute(update(llm_budget).where(llm_budget.c.month == entry["month"])
                                   .values(warn2_sent=True, warn1_sent=True))
                warn = ("budget_warn2", row["warn2_usd"])
            elif new_spent >= row["warn1_usd"] and not row["warn1_sent"]:
                await conn.execute(update(llm_budget).where(llm_budget.c.month == entry["month"]).values(warn1_sent=True))
                warn = ("budget_warn1", row["warn1_usd"])
        if warn and self.alert is not None:
            await self.alert(warn[0], f"Расход на LLM за месяц достиг ${warn[1]:.2f} из ${row['limit_usd']:.2f}")

    async def settle(self, ledger_id: int, cost: Decimal, usage: Usage, stop_reason: str | None) -> None:
        await self._close(ledger_id, state="settled", spent=cost, usage=usage, stop_reason=stop_reason)

    async def release(self, ledger_id: int, error: str) -> None:
        """Запрос точно не был отправлен или отклонён без оплаты."""
        await self._close(ledger_id, state="released", spent=Decimal(0), error=error)

    async def assume_spent(self, ledger_id: int, error: str) -> None:
        """Исход неизвестен: считаем, что запрос оплачен по верхней границе."""
        async with self.engine.connect() as conn:
            reserved = (await conn.execute(select(llm_ledger.c.reserved_usd).where(llm_ledger.c.id == ledger_id))
                        ).scalar_one()
        await self._close(ledger_id, state="assumed_spent", spent=reserved, error=error)

    async def recover_stale(self) -> int:
        """Резервы старше 15 минут → assumed_spent (при старте и по расписанию)."""
        cutoff = self.clock() - STALE_RESERVE
        async with self.engine.connect() as conn:
            ids = (await conn.execute(select(llm_ledger.c.id).where(llm_ledger.c.state == "reserved",
                                                                    llm_ledger.c.started_at < cutoff))).scalars().all()
        for ledger_id in ids:
            await self.assume_spent(ledger_id, "резерв не закрыт: процесс остановился во время вызова")
        if ids:
            log.warning("llm_reserves_recovered", extra={"count": len(ids)})
        return len(ids)

    async def snapshot(self) -> dict:
        """Состояние бюджета для /status и сводок."""
        now = self.clock()
        month = month_start(now)
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(llm_budget).where(llm_budget.c.month == month))).mappings().first()
            today = await self._today_extraction(conn, now)
            agg = (await conn.execute(
                select(func.count(), func.coalesce(func.sum(llm_ledger.c.tokens_cache_read), 0),
                       func.coalesce(func.sum(llm_ledger.c.tokens_in + llm_ledger.c.tokens_cache_write
                                              + llm_ledger.c.tokens_cache_read), 0))
                .where(llm_ledger.c.month == month)
            )).one()
        limit = row["limit_usd"] if row else self.limits.monthly
        spent = row["spent_usd"] if row else Decimal(0)
        reserved = row["reserved_usd"] if row else Decimal(0)
        return {"month": month, "limit": limit, "spent": spent, "reserved": reserved,
                "remaining": limit - spent - reserved, "today_extraction": today,
                "daily_limit": self.limits.daily_extraction, "calls": agg[0],
                "cache_read_share": (Decimal(agg[1]) / Decimal(agg[2])) if agg[2] else Decimal(0)}
