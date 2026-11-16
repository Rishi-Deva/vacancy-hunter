"""Очистка по срокам хранения (DATABASE_SCHEMA.md, раздел 12; ARCHITECTURE.md, раздел 13).

Девять шагов, каждый порциями (`cleanup_batch_rows`), каждая порция — своя транзакция.
Никогда не удаляются: вакансии с отметкой пользователя (со всеми оценками, отметками и письмами),
версии профиля и правил, состояние и проверки источников, помесячные итоги бюджета,
вакансии в `awaiting_description`. Тексты удаляются раньше самих вакансий: пока вакансия хранится
без текста, её ключи дедупликации распознают повторную публикацию как дубль.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.storage.tables import fetch_runs, job_runs, llm_ledger, outbox, raw_items
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.storage.tables import extractions, reports, vacancies

log = logging.getLogger("vacancy_hunter.cleanup")

REJECTED_STATUSES = ("duplicate", "rejected_rules", "rejected_gate", "llm_failed", "closed")


async def _batched(engine: AsyncEngine, table, condition, batch: int, *, action: str = "delete",
                   values: dict | None = None) -> int:
    """Удалить или обновить строки по условию порциями. Возвращает число строк."""
    total = 0
    while True:
        async with engine.begin() as conn:
            ids = select(table.c.id).where(condition).limit(batch).with_for_update(skip_locked=True)
            if action == "delete":
                result = await conn.execute(delete(table).where(table.c.id.in_(ids)))
            else:
                result = await conn.execute(update(table).where(table.c.id.in_(ids)).values(**values))
        total += result.rowcount or 0
        if (result.rowcount or 0) < batch:
            return total


async def cleanup(engine: AsyncEngine, config: AppConfig, *, clock: Clock = utcnow) -> dict:
    r = config.retention
    now = clock()
    batch = r.cleanup_batch_rows
    ago = lambda days: now - timedelta(days=days)  # noqa: E731
    v = vacancies
    no_mark = v.c.user_state == "none"
    has_text = v.c.description_text.isnot(None)
    stats: Counter = Counter()

    # 1. Сырые записи старше 30 дней.
    stats["raw_items"] = await _batched(engine, raw_items, raw_items.c.fetched_at < ago(r.raw_items_days), batch)

    # 2. Текст отклонённых и непоказанных вакансий старше 60 дней.
    not_shown = and_(v.c.status == "scored", v.c.reported_at.is_(None), v.c.title_only_reported_at.is_(None))
    stats["texts_rejected"] = await _batched(
        engine, v, and_(or_(v.c.status.in_(REJECTED_STATUSES), not_shown), no_mark, has_text,
                        v.c.first_seen_at < ago(r.rejected_text_days)),
        batch, action="update", values={"description_text": None, "text_purged_at": now, "updated_at": now})

    # 3. Текст показанных вакансий без отметки старше 180 дней.
    stats["texts_reported"] = await _batched(
        engine, v, and_(v.c.status == "reported", no_mark, has_text, v.c.first_seen_at < ago(r.reported_text_days)),
        batch, action="update", values={"description_text": None, "text_purged_at": now, "updated_at": now})

    # 4. Извлечения, на текст которых не ссылается ни одна вакансия с сохранённым текстом.
    used = exists().where(v.c.description_hash == extractions.c.text_hash, has_text)
    stats["extractions"] = await _batched(engine, extractions, ~used, batch)

    # 5. Вакансии целиком: текст очищен, отметки нет, старше 365 дней. Вместе с ними по каскаду —
    #    источники, оценки, правки, отметки, письма и элементы отчётов.
    #    Без текста — и очищенные, и никогда его не имевшие (например, заголовки hh).
    stats["vacancies"] = await _batched(
        engine, v, and_(v.c.description_text.is_(None),
                        or_(v.c.text_purged_at.isnot(None), v.c.has_description.is_(False)),
                        no_mark, v.c.status != "awaiting_description",
                        v.c.first_seen_at < ago(r.dedup_keys_days)), batch)

    # 6. Отправленные и окончательно неотправленные сообщения старше 30 дней.
    stats["outbox"] = await _batched(
        engine, outbox, and_(outbox.c.state.in_(("sent", "failed")),
                             func.coalesce(outbox.c.sent_at, outbox.c.created_at) < ago(r.outbox_days)), batch)

    # 7. Запуски сборщиков и задач старше 90 дней (кроме выполняющихся).
    stats["fetch_runs"] = await _batched(engine, fetch_runs, and_(fetch_runs.c.started_at < ago(r.runs_days),
                                                                  fetch_runs.c.status != "running"), batch)
    stats["job_runs"] = await _batched(engine, job_runs, and_(job_runs.c.started_at < ago(r.runs_days),
                                                              job_runs.c.status != "running"), batch)

    # 8. Отчёты старше 365 дней (элементы отчётов — по каскаду).
    stats["reports"] = await _batched(engine, reports, reports.c.report_date < ago(r.reports_days).date(), batch)

    # 9. Журнал вызовов LLM старше 13 месяцев; помесячные итоги остаются в llm_budget.
    months = now.year * 12 + now.month - 1 - r.ledger_months
    cutoff = date(months // 12, months % 12 + 1, 1)          # первое число месяца N месяцев назад
    stats["llm_ledger"] = await _batched(engine, llm_ledger, llm_ledger.c.month < cutoff, batch)

    result = {k: n for k, n in stats.items() if n}
    log.info("cleanup_done", extra={"stats": result})
    return result

