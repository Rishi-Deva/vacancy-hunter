"""Уведомление о дооценке (TECHNICAL_SPEC.md, 15.4).

Вакансии, показанные по заголовку и затем получившие полную оценку, в ежедневный отчёт
не возвращаются. Владелец получает одно сообщение не чаще раза в сутки: «Дооценено N вакансий,
из них M получили 70 и более баллов» с кнопкой «Показать». Что уже вошло в уведомление,
отмечается в `app_state` (момент последней учтённой полной оценки).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.notify import enqueue
from hunter_core.scheduler.jobs import local_now
from hunter_core.storage.tables import app_state
from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.storage.tables import scores, vacancies

MARK_KEY = "rescored_notified_until"
GOOD = 70
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def _rescored_query(since: datetime, until: datetime):
    return (select(vacancies.c.id, vacancies.c.title, vacancies.c.company_name, vacancies.c.canonical_url,
                   vacancies.c.first_full_scored_at, scores.c.total)
            .join(scores, and_(scores.c.vacancy_id == vacancies.c.id, scores.c.kind == "full",
                               scores.c.state != "outdated"))
            .where(vacancies.c.title_only_reported_at.isnot(None), vacancies.c.first_full_scored_at > since,
                   vacancies.c.first_full_scored_at <= until)
            .order_by(scores.c.total.desc(), vacancies.c.id))


async def notify_rescored(engine: AsyncEngine, owner_chat_id: int | None, *, timezone: str, clock: Clock = utcnow,
                          wake=None) -> int:
    if owner_chat_id is None:
        return 0
    now = clock()
    async with engine.begin() as conn:
        raw = (await conn.execute(select(app_state.c.value).where(app_state.c.key == MARK_KEY)
                                  .with_for_update())).scalar()
        since = datetime.fromisoformat(raw) if isinstance(raw, str) else EPOCH
        rows = (await conn.execute(_rescored_query(since, now))).mappings().all()
        if not rows:
            return 0
        until = max(r["first_full_scored_at"] for r in rows)
        good = sum(1 for r in rows if r["total"] is not None and r["total"] >= GOOD)
        day = local_now(clock, timezone).date()
        payload = {"text": f"Дооценено {len(rows)} вакансий, из них {good} получили {GOOD} и более баллов.",
                   "link_preview_options": {"is_disabled": True},
                   "reply_markup": {"inline_keyboard": [[{
                       "text": "Показать",
                       "callback_data": f"rs:{_us(since)}:{_us(until)}"}]]}}
        created = await enqueue(conn, dedup_key=f"rescored:{day.isoformat()}", chat_id=owner_chat_id, kind="system",
                                payload=payload, clock=clock)
        if created is None:
            return 0                                   # сегодня уже было: эти вакансии войдут в завтрашнее
        await conn.execute(pg_insert(app_state).values(key=MARK_KEY, value=until.isoformat(), updated_at=now)
                           .on_conflict_do_update(index_elements=[app_state.c.key],
                                                  set_={"value": until.isoformat(), "updated_at": now}))
    if wake is not None:
        wake()
    return len(rows)


def _us(moment: datetime) -> int:
    """Момент в микросекундах: границы списка точно совпадают с учтёнными в уведомлении."""
    return (moment - EPOCH) // timedelta(microseconds=1)


async def rescored_list(conn: AsyncConnection, since_us: int, until_us: int) -> list[dict]:
    since = EPOCH + timedelta(microseconds=since_us)
    until = EPOCH + timedelta(microseconds=until_us)
    return [dict(r) for r in (await conn.execute(_rescored_query(since, until).limit(30))).mappings()]
