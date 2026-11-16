"""Ежедневный отчёт (TECHNICAL_SPEC.md, разделы 14–15; DATABASE_SCHEMA.md, раздел 7).

1. Пауза — отчёт не формируется (сбор и обработка идут).
2. Отчёт за дату уже есть — повторно не формируется (один `daily` на дату).
3. Отбор основного блока и блока «Только по заголовку».
4. Проверка актуальности отобранных (сеть — вне транзакции): закрытые получают статус `closed`
   и заменяются следующими кандидатами; ошибка проверки — пометка, отчёт не задерживается.
5. Одна транзакция: строка `reports`, состав `report_items`, отметки показа (`reported_at`
   основного блока ставится один раз и больше не меняется; `title_only_reported_at` — блока
   по заголовку), сообщения в `outbox`: сводка со звуком, карточки без звука.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.notify import enqueue
from hunter_core.scheduler.jobs import local_now
from hunter_core.storage.tables import outbox
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.reporting.actuality import CLOSED, UNKNOWN, ActualityChecker
from vacancy_hunter.reporting.cards import card_payload
from vacancy_hunter.reporting.select import main_candidates, pick_main, title_only_candidates
from vacancy_hunter.reporting.settings import load_settings
from vacancy_hunter.reporting.summary import collect_stats, summary_text
from vacancy_hunter.storage.tables import report_items, reports, vacancies

log = logging.getLogger("vacancy_hunter.report")


@dataclass
class Built:
    main: list[dict] = field(default_factory=list)
    title_only: list[dict] = field(default_factory=list)
    closed: list[tuple[int, str]] = field(default_factory=list)


def report_window(config: AppConfig, report_time: str) -> tuple[time, time]:
    """Окно отчёта: от времени отчёта на длину догоняющего окна из schedule.yaml (07:00–14:00 → 7 часов)."""
    base_start = datetime.combine(datetime.min, time.fromisoformat(config.schedule.report_time))
    base_end = datetime.combine(datetime.min, time.fromisoformat(config.schedule.report_catchup_until))
    start = datetime.combine(datetime.min, time.fromisoformat(report_time))
    end = start + (base_end - base_start)
    return start.time(), (end.time() if end.date() == start.date() else time(23, 59, 59))


async def report_due(engine: AsyncEngine, config: AppConfig, clock: Clock = utcnow) -> bool:
    """Пора ли формировать отчёт: не пауза, время в окне, сегодняшнего отчёта ещё нет.

    Проверяется каждую минуту: смена времени через /settings действует без перезапуска,
    а после перезапуска внутри окна отчёт формируется догоняющим запуском.
    """
    settings_ = await load_settings(engine, config)
    if settings_.paused:
        return False
    local = local_now(clock, config.schedule.timezone)
    start, end = report_window(config, settings_.report_time)
    if not start <= local.time().replace(tzinfo=None) < end:
        return False
    return not await report_exists(engine, local.date())


ATTEMPT_KEY = "report.last_attempt"
RETRY_MINUTES = 15


async def claim_attempt(engine: AsyncEngine, clock: Clock = utcnow, minutes: int = RETRY_MINUTES) -> bool:
    """Не чаще одной попытки за N минут: при устойчивой ошибке отчёт не повторяется каждую минуту."""
    from hunter_core.storage.tables import app_state

    now = clock()
    async with engine.begin() as conn:
        last = (await conn.execute(select(app_state.c.value).where(app_state.c.key == ATTEMPT_KEY)
                                   .with_for_update())).scalar()
        if isinstance(last, str) and now - datetime.fromisoformat(last) < timedelta(minutes=minutes):
            return False
        await conn.execute(pg_insert(app_state).values(key=ATTEMPT_KEY, value=now.isoformat(), updated_at=now)
                           .on_conflict_do_update(index_elements=[app_state.c.key],
                                                  set_={"value": now.isoformat(), "updated_at": now}))
    return True


async def report_exists(engine: AsyncEngine, report_date) -> bool:
    async with engine.connect() as conn:
        return (await conn.execute(select(reports.c.id).where(reports.c.kind == "daily",
                                                              reports.c.report_date == report_date))).first() \
            is not None


async def _check_all(candidates: list[dict], limit: int, checker: ActualityChecker, today, built: Built,
                     per_company: int) -> list[dict]:
    """Идти по кандидатам в порядке отбора, пропуская закрытые, пока не наберётся лимит."""
    accepted: list[dict] = []
    for candidate in candidates:
        if len(pick_main(accepted + [candidate], limit, per_company)) == len(accepted):
            continue                                   # лимит компании или общий лимит
        result = await checker.check(candidate, today)
        if result.state == CLOSED:
            built.closed.append((candidate["vacancy"]["id"], result.reason or "закрыта"))
            continue
        candidate["notes"] = [result.note] if result.note else []
        candidate["not_checked"] = result.state == UNKNOWN
        accepted.append(candidate)
        if len(accepted) >= limit:
            break
    return accepted


async def select_report(engine: AsyncEngine, config: AppConfig, now: datetime, checker: ActualityChecker) -> Built:
    settings_ = await load_settings(engine, config)
    today = local_now(lambda: now, config.schedule.timezone).date()
    async with engine.connect() as conn:
        main = await main_candidates(conn, config, settings_, now)
        title_only = await title_only_candidates(conn, config, now)
    built = Built()
    built.main = await _check_all(main, settings_.max_cards, checker, today, built,
                                  config.scoring.report.max_per_company)
    built.title_only = await _check_all(title_only, config.scoring.report.title_only_max, checker, today, built,
                                        per_company=10**6)
    return built


async def build_daily_report(engine: AsyncEngine, config: AppConfig, *, checker: ActualityChecker,
                             owner_chat_id: int | None, clock: Clock = utcnow, budget_remaining=None,
                             wake=None, dry_run: bool = False) -> dict:
    now = clock()
    report_date = local_now(clock, config.schedule.timezone).date()
    settings_ = await load_settings(engine, config)
    if settings_.paused and not dry_run:
        return {"skipped": "paused"}
    if not dry_run and owner_chat_id is None:
        # Без получателя отчёт не формируется: иначе вакансии получили бы отметку о показе,
        # которого не было, и по правилу первого показа больше не попали бы в отчёт.
        log.warning("report_skipped_no_owner", extra={"hint": "TELEGRAM_OWNER_ID не задан"})
        return {"skipped": "no_owner"}
    if not dry_run and await report_exists(engine, report_date):
        return {"skipped": "exists"}
    built = await select_report(engine, config, now, checker)
    not_checked = sum(1 for c in built.main + built.title_only if c.get("not_checked"))
    async with engine.connect() as conn:
        stats = await collect_stats(conn, now, report_date, built.main, built.title_only,
                                    budget_remaining=budget_remaining, closed=len(built.closed),
                                    not_checked=not_checked)
    header = {"text": summary_text(stats), "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    cards = [(block, c, card_payload(c["vacancy"], c["score"], c["url"], c.get("notes")))
             for block, items in (("main", built.main), ("title_only", built.title_only)) for c in items]
    if dry_run:
        return {"dry_run": True, "header": header["text"], "cards": [p["text"] for _, _, p in cards],
                "closed": built.closed}
    skipped = 0
    async with engine.begin() as conn:
        report_id = (await conn.execute(pg_insert(reports).values(
            kind="daily", report_date=report_date, status="building", stats=stats, created_at=now,
        ).on_conflict_do_nothing().returning(reports.c.id))).scalar()
        if report_id is None:                              # параллельный запуск успел раньше
            return {"skipped": "exists"}
        for vacancy_id, reason in built.closed:
            await conn.execute(update(vacancies).where(
                vacancies.c.id == vacancy_id, vacancies.c.status == "scored",
            ).values(status="closed", status_reason=f"actuality: {reason}"[:200], closed_at=now, updated_at=now))
        await enqueue(conn, dedup_key=f"report:{report_id}:header", chat_id=owner_chat_id,
                      kind="report_header", payload=header, clock=clock)
        positions = {"main": 0, "title_only": 0}
        for block, c, payload in cards:
            vid = c["vacancy"]["id"]
            # Отметка о показе ставится, только если за время проверки актуальности вакансию не отклонили,
            # не показали и конвейер не сменил её статус; иначе карточка не входит в отчёт.
            still = [vacancies.c.id == vid, vacancies.c.status == "scored", vacancies.c.user_state != "rejected",
                     vacancies.c.reported_at.is_(None), vacancies.c.title_only_reported_at.is_(None)]
            if block == "main":
                marked = await conn.execute(update(vacancies).where(*still).values(
                    status="reported", reported_at=now, updated_at=now))
            else:
                marked = await conn.execute(update(vacancies).where(*still).values(
                    title_only_reported_at=now, updated_at=now))
            if not marked.rowcount:
                skipped += 1
                continue
            positions[block] += 1
            item_id = (await conn.execute(report_items.insert().values(
                report_id=report_id, vacancy_id=vid, block=block, position=positions[block],
                score_id=c["score"]["id"], total_snapshot=c["score"]["total"],
            ).returning(report_items.c.id))).scalar_one()
            await enqueue(conn, dedup_key=f"report:{report_id}:item:{vid}", chat_id=owner_chat_id,
                          kind="vacancy_card", payload=payload, clock=clock, report_item_id=item_id)
        await conn.execute(update(reports).where(reports.c.id == report_id).values(
            status="ready", completed_at=clock()))
        await _close_previous(conn, report_id)
    if wake is not None:
        wake()
    result = {"report_id": report_id, "main": positions["main"], "title_only": positions["title_only"],
              "closed": len(built.closed), "not_checked": not_checked, "changed_meanwhile": skipped}
    log.info("report_built", extra=result)
    return result


async def _close_previous(conn, current_id: int) -> None:
    """Отчёты прошлых дней: `sent`, если все сообщения подтверждены, иначе `partial`."""
    rows = (await conn.execute(select(reports.c.id).where(reports.c.kind == "daily", reports.c.status == "ready",
                                                          reports.c.id != current_id))).scalars().all()
    for rid in rows:
        items = select(report_items.c.id).where(report_items.c.report_id == rid)
        states = set((await conn.execute(select(outbox.c.state).where(
            outbox.c.report_item_id.in_(items) | (outbox.c.dedup_key == f"report:{rid}:header"),
            ~outbox.c.dedup_key.contains(":req:")))).scalars())
        if states and states <= {"sent"}:
            await conn.execute(update(reports).where(reports.c.id == rid).values(status="sent"))
        elif states & {"failed", "unconfirmed"} and not states & {"pending", "sending"}:
            await conn.execute(update(reports).where(reports.c.id == rid).values(status="partial"))
