"""Версии профиля и правил скоринга (DATABASE_SCHEMA.md, раздел 5; TECHNICAL_SPEC.md, раздел 19).

Хеш считается по проверенному содержимому файлов, а не по тексту: правка комментария
новой версии не создаёт. Версия профиля = profile.yaml (без контактов: в оценке они не
участвуют) + skills.yaml; версия правил = scoring.yaml. Таблицы только пополняются.

Новая или вновь выбранная версия становится текущей, а действующие оценки вакансий,
подлежащих пересчёту, переводятся в `pending_recalc` (переход 1 раздела 6.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.storage.tables import profile_versions, scores, scoring_versions, vacancies

log = logging.getLogger("vacancy_hunter.versions")

RECALC_STATUSES = ("scored", "rejected_gate", "reported")
_LOCK_KEY = 7_201_126    # pg_advisory_xact_lock: одна проверка версий за раз


@dataclass(frozen=True)
class Versions:
    profile_id: int
    scoring_id: int
    profile_no: int
    scoring_no: int
    changed: bool = False
    marked: int = 0       # сколько оценок переведено в pending_recalc


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def profile_snapshot(config: AppConfig) -> dict:
    data = config.profile.model_dump(mode="json")
    data["professional"].pop("contacts", None)
    return data


def skills_snapshot(config: AppConfig) -> dict:
    return config.skills.model_dump(mode="json")


def scoring_snapshot(config: AppConfig) -> dict:
    return config.scoring.model_dump(mode="json")


async def _ensure(conn: AsyncConnection, table, content_hash: str, values: dict, now) -> tuple[int, int, bool]:
    row = (await conn.execute(select(table.c.id, table.c.version_no, table.c.is_current)
                              .where(table.c.content_hash == content_hash))).first()
    if row is not None and row.is_current:
        return row.id, row.version_no, False
    await conn.execute(update(table).where(table.c.is_current).values(is_current=False))
    if row is not None:              # возврат к прежнему содержимому — прежняя версия снова текущая
        await conn.execute(update(table).where(table.c.id == row.id).values(is_current=True))
        return row.id, row.version_no, True
    number = (await conn.execute(select(func.coalesce(func.max(table.c.version_no), 0)))).scalar_one() + 1
    new_id = (await conn.execute(table.insert().values(
        version_no=number, content_hash=content_hash, is_current=True, created_at=now, **values,
    ).returning(table.c.id))).scalar_one()
    return new_id, number, True


async def mark_for_recalc(conn: AsyncConnection, profile_id: int, scoring_id: int, now) -> int:
    """Действующие оценки с прежними версиями → pending_recalc (только подлежащие пересчёту)."""
    eligible = and_(
        scores.c.vacancy_id == vacancies.c.id,
        vacancies.c.status.in_(RECALC_STATUSES),
        or_(scores.c.kind == "preliminary",
            and_(scores.c.extraction_id.isnot(None), vacancies.c.description_text.isnot(None))),
    )
    result = await conn.execute(
        update(scores).where(scores.c.state == "current", eligible,
                             or_(scores.c.profile_version_id != profile_id, scores.c.scoring_version_id != scoring_id))
        .values(state="pending_recalc")
    )
    return result.rowcount or 0


async def ensure_versions(engine: AsyncEngine, config: AppConfig, *, clock: Clock = utcnow) -> Versions:
    now = clock()
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        p_snap, s_snap = profile_snapshot(config), skills_snapshot(config)
        p_id, p_no, p_changed = await _ensure(
            conn, profile_versions, _hash({"profile": p_snap, "skills": s_snap}),
            {"profile": p_snap, "skills_dictionary": s_snap, "source_note": config.profile.source_note}, now)
        rules = scoring_snapshot(config)
        s_id, s_no, s_changed = await _ensure(conn, scoring_versions, _hash(rules), {"rules": rules}, now)
        changed = p_changed or s_changed
        marked = await mark_for_recalc(conn, p_id, s_id, now) if changed else 0
    if changed:
        log.info("versions_changed", extra={"profile_version": p_no, "scoring_version": s_no,
                                            "scores_to_recalc": marked})
    return Versions(p_id, s_id, p_no, s_no, changed, marked)
