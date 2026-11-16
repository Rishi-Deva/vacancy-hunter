"""/stats: показатели за 7 и 30 дней раздельно по типам предложения (TECHNICAL_SPEC.md, 11.10, 16.2)."""

from __future__ import annotations

import html
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from vacancy_hunter.reporting.feedback import REASONS
from vacancy_hunter.reporting.summary import OFFER_TYPES
from vacancy_hunter.storage.tables import feedback_events, scores, vacancies

TYPE_TITLE = {"job": "Работа", "internship": "Стажировки", "project": "Разовые проекты", "unknown": "Тип не определён"}
CATEGORY_SHORT = {"excellent": "отличное", "good": "хорошее", "consider": "можно рассмотреть", "hidden": "скрыто"}
FILTERS = {"all": None, "job": "job", "internship": "internship", "project": "project"}


def _type(value: str | None) -> str:
    return value if value in OFFER_TYPES else "unknown"


async def _by_type(conn: AsyncConnection, query) -> dict[str, int]:
    result: dict[str, int] = {}
    for t, n in (await conn.execute(query)).all():
        result[_type(t)] = result.get(_type(t), 0) + n
    return result


async def period_stats(conn: AsyncConnection, since: datetime) -> dict[str, dict]:
    v = vacancies
    collected = await _by_type(conn, select(v.c.offer_type, func.count()).where(
        v.c.first_seen_at >= since, v.c.status != "duplicate").group_by(v.c.offer_type))
    shown = await _by_type(conn, select(v.c.offer_type, func.count()).where(v.c.reported_at >= since)
                           .group_by(v.c.offer_type))
    title_only = await _by_type(conn, select(v.c.offer_type, func.count())
                                .where(v.c.title_only_reported_at >= since).group_by(v.c.offer_type))
    categories: dict[str, dict[str, int]] = {}
    for t, cat, n in (await conn.execute(select(v.c.offer_type, scores.c.category, func.count())
                                         .join(scores, and_(scores.c.vacancy_id == v.c.id, scores.c.kind == "full",
                                                            scores.c.state != "outdated"))
                                         .where(v.c.first_seen_at >= since)
                                         .group_by(v.c.offer_type, scores.c.category))).all():
        categories.setdefault(_type(t), {})[cat] = n
    events: dict[str, dict[str, int]] = {}
    reasons: dict[str, dict[str, int]] = {}
    for t, event, reason, n in (await conn.execute(
            select(v.c.offer_type, feedback_events.c.event, feedback_events.c.reason_code, func.count())
            .join(v, v.c.id == feedback_events.c.vacancy_id).where(feedback_events.c.created_at >= since)
            .group_by(v.c.offer_type, feedback_events.c.event, feedback_events.c.reason_code))).all():
        events.setdefault(_type(t), {})
        events[_type(t)][event] = events[_type(t)].get(event, 0) + n
        if event == "rejected" and reason:
            reasons.setdefault(_type(t), {})[reason] = n
    result = {}
    for t in OFFER_TYPES:
        result[t] = {"collected": collected.get(t, 0), "shown": shown.get(t, 0), "title_only": title_only.get(t, 0),
                     "categories": categories.get(t, {}), "events": events.get(t, {}), "reasons": reasons.get(t, {})}
    return result


def _type_lines(t: str, s: dict) -> list[str]:
    cats = ", ".join(f"{CATEGORY_SHORT[c]} {s['categories'].get(c, 0)}" for c in CATEGORY_SHORT)
    e = s["events"]
    line = (f"<b>{TYPE_TITLE[t]}</b>: собрано {s['collected']}, показано {s['shown']}; {cats}; "
            f"интересно {e.get('interested', 0)}, откликнулся {e.get('applied', 0)}, "
            f"отклонено {e.get('rejected', 0)}")
    lines = [line]
    if s["reasons"]:
        lines.append("  причины: " + ", ".join(f"{REASONS[r]} {n}" for r, n in sorted(s["reasons"].items())))
    lines.append(f"  только по заголовку: показано {s['title_only']}")
    return lines


async def stats_text(conn: AsyncConnection, now: datetime, offer_filter: str | None = None) -> str:
    lines = ["<b>Статистика</b>" + (f" — {html.escape(TYPE_TITLE[offer_filter].lower())}" if offer_filter else "")]
    for days in (7, 30):
        data = await period_stats(conn, now - timedelta(days=days))
        lines += ["", f"<b>За {days} дней</b>"]
        for t in OFFER_TYPES:
            if offer_filter and t != offer_filter:
                continue
            if t == "unknown" and not offer_filter and not any(
                    data[t][k] for k in ("collected", "shown", "title_only")):
                continue
            lines += _type_lines(t, data[t])
    return "\n".join(lines)


def filter_keyboard(prefix: str) -> list[list[dict]]:
    return [[{"text": "Все", "callback_data": f"{prefix}:all"}, {"text": "Работа", "callback_data": f"{prefix}:job"},
             {"text": "Стажировки", "callback_data": f"{prefix}:internship"},
             {"text": "Проекты", "callback_data": f"{prefix}:project"}]]
