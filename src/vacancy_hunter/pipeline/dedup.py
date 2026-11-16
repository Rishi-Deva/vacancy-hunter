"""Этап 3 конвейера: дедупликация (TECHNICAL_SPEC.md, раздел 7; FR-6).

Уровни по порядку:
1. совпадает каноническая ссылка — окно 365 дней;
2. совпадает хеш нормализованного текста — 365 дней;
3. совпадает нормализованная компания и схожесть названий не ниже 0,85 — 30 дней
   (не применяется, если компания не определена).

При совпадении:
- новая запись получает статус `duplicate` и ссылку на оригинал; её источник
  переносится оригиналу в `vacancy_sources`;
- если у оригинала нет описания, а у дубля есть — описание переносится оригиналу,
  и оригинал проходит полный конвейер заново (статус `new`);
- если оригинал известен только из тихих источников, а дубль — из активного, `quiet_only` снимается;
- показанная или отклонённая пользователем вакансия повторно не показывается: дубль
  никогда не попадает в отчёт, а у оригинала отметки показа не меняются.
Ключи дедупликации сохраняются и после очистки текста (DATABASE_SCHEMA.md, раздел 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from vacancy_hunter.storage.tables import vacancies, vacancy_sources

SIMILARITY_THRESHOLD = 0.85
LONG_WINDOW = timedelta(days=365)
SHORT_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class Match:
    original_id: int
    level: int     # 1 — ссылка, 2 — текст, 3 — компания и название


def _candidates(vacancy: dict, since: datetime):
    # Оригинал — всегда более ранняя запись: порядок обработки не меняет, кто из двух дубль.
    return and_(
        vacancies.c.id < vacancy["id"],
        vacancies.c.duplicate_of.is_(None),
        vacancies.c.status != "duplicate",
        vacancies.c.first_seen_at >= since,
    )


async def find_original(conn: AsyncConnection, vacancy: dict, now: datetime) -> Match | None:
    base_long = _candidates(vacancy, now - LONG_WINDOW)
    if vacancy.get("canonical_url"):
        found = (await conn.execute(
            select(vacancies.c.id).where(base_long, vacancies.c.canonical_url == vacancy["canonical_url"])
            .order_by(vacancies.c.id).limit(1)
        )).scalar()
        if found:
            return Match(found, 1)
    if vacancy.get("description_hash"):
        found = (await conn.execute(
            select(vacancies.c.id).where(base_long, vacancies.c.description_hash == vacancy["description_hash"])
            .order_by(vacancies.c.id).limit(1)
        )).scalar()
        if found:
            return Match(found, 2)
    if vacancy.get("company_norm") and vacancy.get("title_norm"):
        similarity = func.similarity(vacancies.c.title_norm, vacancy["title_norm"])
        found = (await conn.execute(
            select(vacancies.c.id)
            .where(_candidates(vacancy, now - SHORT_WINDOW),
                   vacancies.c.company_norm == vacancy["company_norm"],
                   similarity >= SIMILARITY_THRESHOLD)
            .order_by(similarity.desc(), vacancies.c.id).limit(1)
        )).scalar()
        if found:
            return Match(found, 3)
    return None


async def mark_duplicate(conn: AsyncConnection, vacancy: dict, match: Match, now: datetime) -> None:
    original = dict((await conn.execute(
        select(vacancies).where(vacancies.c.id == match.original_id).with_for_update()
    )).mappings().one())

    await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
        status="duplicate", status_reason=f"dedup_level_{match.level}", duplicate_of=original["id"],
        lease_until=None, updated_at=now,
    ))
    # Источник дубля становится источником оригинала (вторая линия защиты от повторной обработки).
    await conn.execute(update(vacancy_sources).where(vacancy_sources.c.vacancy_id == vacancy["id"])
                       .values(vacancy_id=original["id"]))

    changes: dict = {"last_seen_at": now, "updated_at": now}
    if original["quiet_only"] and not vacancy["quiet_only"]:
        changes["quiet_only"] = False
    if not original["has_description"] and vacancy["has_description"] and vacancy.get("description_text"):
        changes.update(description_text=vacancy["description_text"], description_hash=vacancy["description_hash"],
                       has_description=True)
        if original["status"] in ("new", "awaiting_description", "scored", "rejected_rules"):
            # Описание появилось — оригинал проходит полный конвейер в той же строке (без дубля).
            changes.update(status="new", status_reason="description_from_duplicate")
    await conn.execute(update(vacancies).where(vacancies.c.id == original["id"]).values(**changes))
