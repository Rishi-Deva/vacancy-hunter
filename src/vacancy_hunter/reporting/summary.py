"""Сводка отчёта (TECHNICAL_SPEC.md, 15.3): всегда первым сообщением; показатели — раздельно по типам."""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from hunter_core.storage.tables import outbox, raw_items, source_state
from vacancy_hunter.storage.tables import report_items, reports, vacancies

OFFER_TYPES = ("job", "internship", "project", "unknown")
TYPE_TEXT = {"job": "работа", "internship": "стажировки", "project": "проекты", "unknown": "тип не определён"}


def by_type_text(counts: dict[str, int]) -> str:
    """«работа 3, стажировки 1, проекты 0» — тип «не определён» показывается, только если он есть."""
    parts = [f"{TYPE_TEXT[t]} {counts.get(t, 0)}" for t in OFFER_TYPES if t != "unknown" or counts.get(t)]
    return ", ".join(parts)


def count_types(items: list[dict]) -> dict[str, int]:
    result = {t: 0 for t in OFFER_TYPES}
    for item in items:
        t = item["vacancy"].get("offer_type") or "unknown"
        result[t if t in result else "unknown"] += 1
    return result


async def _types(conn: AsyncConnection, *where) -> dict[str, int]:
    rows = (await conn.execute(select(vacancies.c.offer_type, func.count()).where(*where)
                               .group_by(vacancies.c.offer_type))).all()
    result = {t: 0 for t in OFFER_TYPES}
    for t, n in rows:
        result[t if t in result else "unknown"] += n
    return result


async def unconfirmed_of_previous(conn: AsyncConnection, report_date: date) -> int:
    prev = (await conn.execute(select(reports.c.id).where(reports.c.kind == "daily",
                                                          reports.c.report_date < report_date)
                               .order_by(reports.c.report_date.desc()).limit(1))).scalar()
    if prev is None:
        return 0
    item_ids = select(report_items.c.id).where(report_items.c.report_id == prev)
    return (await conn.execute(select(func.count()).select_from(outbox).where(
        outbox.c.state.in_(("unconfirmed", "failed")), ~outbox.c.dedup_key.contains(":req:"),
        (outbox.c.report_item_id.in_(item_ids)) | (outbox.c.dedup_key == f"report:{prev}:header"),
    ))).scalar_one()


async def collect_stats(conn: AsyncConnection, now: datetime, report_date: date, main: list[dict],
                        title_only: list[dict], *, budget_remaining=None, closed: int = 0,
                        not_checked: int = 0) -> dict:
    day_ago = now - timedelta(hours=24)
    fresh = vacancies.c.first_seen_at >= day_ago
    problems = (await conn.execute(select(source_state.c.code, source_state.c.health).where(
        source_state.c.mode.in_(("active", "quiet")),
        (source_state.c.health != "ok") | (source_state.c.consecutive_failures > 0),
    ).order_by(source_state.c.code))).all()
    warnings = []
    if not_checked:
        warnings.append(f"актуальность не проверена для {not_checked}")
    if closed:
        warnings.append(f"закрыто при проверке перед отчётом: {closed}")
    return {
        "date": report_date.isoformat(),
        "main": count_types(main), "title_only": count_types(title_only),
        "collected": (await conn.execute(select(func.count()).select_from(raw_items)
                                         .where(raw_items.c.fetched_at >= day_ago))).scalar_one(),
        "new": await _types(conn, fresh, vacancies.c.status != "duplicate"),
        "rejected_rules": await _types(conn, fresh, vacancies.c.status == "rejected_rules"),
        "rejected_gate": await _types(conn, fresh, vacancies.c.status == "rejected_gate"),
        "pending": (await conn.execute(select(func.count()).select_from(vacancies).where(
            vacancies.c.status.in_(("new", "pending_llm", "extracted"))))).scalar_one(),
        "sources_problem": [f"{code} ({health})" for code, health in problems],
        "budget_remaining": None if budget_remaining is None else float(budget_remaining),
        "unconfirmed_prev": await unconfirmed_of_previous(conn, report_date),
        "warnings": warnings,
    }


def summary_text(stats: dict) -> str:
    d = date.fromisoformat(stats["date"])
    main_n, title_n = sum(stats["main"].values()), sum(stats["title_only"].values())
    lines = [f"<b>Отчёт за {d:%d.%m.%Y}</b>"]
    if main_n == 0 and title_n == 0:
        lines.append("Подходящих вакансий сегодня нет.")
    else:
        lines.append(f"Карточек: {main_n} ({by_type_text(stats['main'])})")
        if title_n:
            lines.append(f"Только по заголовку: {title_n} ({by_type_text(stats['title_only'])})")
    lines += [
        "",
        f"За сутки собрано записей: {stats['collected']}",
        f"Новых вакансий: {sum(stats['new'].values())} ({by_type_text(stats['new'])})",
        f"Отклонено правилами: {sum(stats['rejected_rules'].values())} ({by_type_text(stats['rejected_rules'])})",
        f"Отклонено фильтрами: {sum(stats['rejected_gate'].values())} ({by_type_text(stats['rejected_gate'])})",
        f"Ждут анализа: {stats['pending']}",
    ]
    if stats["sources_problem"]:
        lines.append("Источники с проблемами: " + html.escape(", ".join(stats["sources_problem"])))
    if stats["budget_remaining"] is not None:
        lines.append(f"Бюджет LLM: остаток ${stats['budget_remaining']:.2f}")
    if stats["unconfirmed_prev"]:
        lines.append(f"Не подтверждена отправка {stats['unconfirmed_prev']} сообщений прошлого отчёта — /today")
    if stats["warnings"]:
        lines.append("Предупреждения: " + html.escape("; ".join(stats["warnings"])))
    return "\n".join(lines)
