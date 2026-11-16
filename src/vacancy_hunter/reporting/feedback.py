"""Отметки пользователя (DATABASE_SCHEMA.md, 8.1; TECHNICAL_SPEC.md, 16.3).

Журнал `feedback_events` только пополняется. `vacancies.user_state` — текущее состояние,
обновляется в той же транзакции. Повторное нажатие той же кнопки второго события не создаёт
(«уже отмечено»): строка вакансии блокируется, состояние сверяется под блокировкой.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.storage.tables import feedback_events, scores, vacancies

EVENT_STATE = {"interested": "interested", "applied": "applied", "rejected": "rejected", "cleared": "none"}
REASONS = {"stack": "не тот стек", "level": "уровень", "not_remote": "не удалённо", "company": "компания",
           "sales": "продажи", "conditions": "условия", "seen": "уже видел", "other": "другое"}
EVENT_TEXT = {"interested": "Отмечено: интересно", "applied": "Отмечено: откликнулся",
              "rejected": "Отклонено", "cleared": "Отметка снята"}
STATE_TEXT = {"interested": "интересно", "applied": "откликнулся", "rejected": "отклонено", "none": "без отметки"}

RECORDED, ALREADY, MISSING = "recorded", "already", "missing"


async def current_score_id(conn, vacancy_id: int) -> int | None:
    rows = (await conn.execute(select(scores.c.id, scores.c.kind).where(
        scores.c.vacancy_id == vacancy_id, scores.c.state != "outdated"))).all()
    by_kind = {kind: sid for sid, kind in rows}
    return by_kind.get("full") or by_kind.get("preliminary")


async def record(engine: AsyncEngine, vacancy_id: int, event: str, *, reason: str | None = None,
                 comment: str | None = None, clock: Clock = utcnow) -> str:
    if event not in EVENT_STATE or (reason is not None and reason not in REASONS):
        raise ValueError(f"недопустимое событие {event}/{reason}")
    async with engine.begin() as conn:
        state = (await conn.execute(select(vacancies.c.user_state).where(vacancies.c.id == vacancy_id)
                                    .with_for_update())).scalar()
        if state is None:
            return MISSING
        if state == EVENT_STATE[event]:
            return ALREADY
        now = clock()
        await conn.execute(feedback_events.insert().values(
            vacancy_id=vacancy_id, event=event, reason_code=reason if event == "rejected" else None,
            comment=comment, score_id=await current_score_id(conn, vacancy_id), created_at=now))
        await conn.execute(update(vacancies).where(vacancies.c.id == vacancy_id).values(
            user_state=EVENT_STATE[event], user_state_at=now))
    return RECORDED
