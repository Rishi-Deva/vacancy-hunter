"""Этап 6 конвейера: фильтры и скоринг (ARCHITECTURE.md, разделы 4, 9; TECHNICAL_SPEC.md, разделы 10–12).

- `extracted` → полная оценка по фактам извлечения → `scored` или `rejected_gate`;
  при первой успешной полной оценке записывается `first_full_scored_at` (один раз);
- `new` с пометкой «ждёт предварительной оценки» (вакансия без описания, без слова уровня)
  → оценка по заголовку → `scored` (или `rejected_gate` по структурному полю).

Оценка — новая строка `scores` по текущим версиям профиля и правил. Прежняя действующая
строка того же вида получает `outdated`, `superseded_by` и `superseded_at` (история не теряется).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.timeutil import Clock, utcnow
from vacancy_hunter.config import AppConfig
from vacancy_hunter.matching import MatchProfile, ScoreResult, score_full, score_preliminary
from vacancy_hunter.matching.versions import Versions, ensure_versions
from vacancy_hunter.pipeline.prefilter import PRELIMINARY_PENDING
from vacancy_hunter.storage.tables import extractions, scores, vacancies

log = logging.getLogger("vacancy_hunter.score")

BATCH = 200


async def find_extraction(conn: AsyncConnection, vacancy: dict, config: AppConfig) -> dict | None:
    """Извлечение для текста вакансии: сначала по текущим модели и промпту, иначе последнее готовое."""
    if not vacancy.get("description_hash"):
        return None
    role = config.llm.roles.extraction
    rows = (await conn.execute(
        select(extractions).where(extractions.c.text_hash == vacancy["description_hash"],
                                  extractions.c.status == "ok")
        .order_by((extractions.c.model == role.model) & (extractions.c.prompt_version == role.prompt_version),
                  extractions.c.created_at, extractions.c.id)
    )).mappings().all()
    return dict(rows[-1]) if rows else None


async def store_score(conn: AsyncConnection, vacancy_id: int, result: ScoreResult, versions: Versions,
                      extraction_id: int | None, now: datetime) -> int:
    """Новая действующая строка; прежняя действующая того же вида → outdated.

    Действующие строки блокируются: параллельный пересчёт той же вакансии дождётся
    и увидит уже устаревшую строку, а не создаст вторую действующую.
    """
    old_ids = (await conn.execute(
        select(scores.c.id).where(scores.c.vacancy_id == vacancy_id, scores.c.kind == result.kind,
                                  scores.c.state != "outdated").with_for_update()
    )).scalars().all()
    if old_ids:
        await conn.execute(update(scores).where(scores.c.id.in_(old_ids), scores.c.state != "outdated")
                           .values(state="outdated", superseded_at=now))
    new_id = (await conn.execute(scores.insert().values(
        vacancy_id=vacancy_id, state="current", extraction_id=extraction_id,
        profile_version_id=versions.profile_id, scoring_version_id=versions.scoring_id, created_at=now,
        **result.row(),
    ).returning(scores.c.id))).scalar_one()
    if old_ids:
        await conn.execute(update(scores).where(scores.c.id.in_(old_ids), scores.c.superseded_by.is_(None))
                           .values(superseded_by=new_id))
    return new_id


async def has_active_full(conn: AsyncConnection, vacancy_id: int) -> bool:
    return (await conn.execute(select(scores.c.id).where(
        scores.c.vacancy_id == vacancy_id, scores.c.kind == "full", scores.c.state != "outdated").limit(1)
    )).first() is not None


def gate_reason(result: ScoreResult) -> str | None:
    return ("gate:" + ",".join(result.rejected_by)) if result.rejected_by else None


def compute(vacancy: dict, extraction: dict | None, kind: str, profile: MatchProfile, config: AppConfig,
            now: datetime) -> ScoreResult:
    if kind == "preliminary":
        return score_preliminary(vacancy, profile, config.scoring, now)
    if extraction is None or not extraction.get("facts"):
        raise LookupError("нет готового извлечения для текста вакансии")
    return score_full(vacancy, extraction["facts"], profile, config.scoring, now,
                      quote_check=extraction.get("quote_check"))


async def score_pending(engine: AsyncEngine, config: AppConfig, *, clock: Clock = utcnow,
                        versions: Versions | None = None) -> dict:
    versions = versions or await ensure_versions(engine, config, clock=clock)
    profile = MatchProfile.from_config(config)
    stats: Counter = Counter()
    await _full(engine, config, profile, versions, clock, stats)
    await _preliminary(engine, config, profile, versions, clock, stats)
    result = dict(stats)
    if result:
        log.info("scoring_done", extra={"stats": result})
    return result


async def _full(engine, config, profile, versions, clock, stats) -> None:
    last_id = 0
    while True:
        async with engine.begin() as conn:
            rows = (await conn.execute(
                select(vacancies).where(vacancies.c.status == "extracted", vacancies.c.id > last_id)
                .order_by(vacancies.c.id).limit(BATCH).with_for_update(skip_locked=True)
            )).mappings().all()
            if not rows:
                return
            now = clock()
            for row in rows:
                vacancy = dict(row)
                last_id = vacancy["id"]
                try:
                    async with conn.begin_nested():
                        key = await _score_full_one(conn, vacancy, config, profile, versions, now)
                except Exception as exc:   # одна запись не останавливает оценку остальных
                    await _mark_error(conn, vacancy, exc, now)
                    stats["error"] += 1
                    continue
                stats[key] += 1


async def _preliminary(engine, config, profile, versions, clock, stats) -> None:
    last_id = 0
    while True:
        async with engine.begin() as conn:
            rows = (await conn.execute(
                select(vacancies).where(vacancies.c.status == "new", vacancies.c.status_reason == PRELIMINARY_PENDING,
                                        vacancies.c.id > last_id)
                .order_by(vacancies.c.id).limit(BATCH).with_for_update(skip_locked=True)
            )).mappings().all()
            if not rows:
                return
            now = clock()
            for row in rows:
                vacancy = dict(row)
                last_id = vacancy["id"]
                try:
                    async with conn.begin_nested():
                        result = compute(vacancy, None, "preliminary", profile, config, now)
                        await store_score(conn, vacancy["id"], result, versions, None, now)
                        passed = result.gate_result == "passed"
                        offer_type = result.explanation["offer_type"]
                        await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
                            status="scored" if passed else "rejected_gate", status_reason=gate_reason(result),
                            offer_type=offer_type if offer_type in ("job", "internship", "project")
                            else vacancy["offer_type"],
                            updated_at=now,
                        ))
                except Exception as exc:   # причина остаётся «ждёт предварительной оценки»: повтор в следующий раз
                    log.warning("score_failed", extra={"vacancy_id": vacancy["id"],
                                                       "error": f"{type(exc).__name__}: {exc}"[:300]})
                    stats["error"] += 1
                    continue
                stats[f"preliminary:{result.category}" if passed else "preliminary:rejected_gate"] += 1


async def _score_full_one(conn, vacancy, config, profile, versions, now) -> str:
    extraction = await find_extraction(conn, vacancy, config)
    if extraction is None:
        # Текст изменился или извлечение очищено: запись снова идёт на извлечение.
        await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
            status="pending_llm", status_reason="no_extraction", updated_at=now))
        return "no_extraction"
    result = compute(vacancy, extraction, "full", profile, config, now)
    await store_score(conn, vacancy["id"], result, versions, extraction["id"], now)
    passed = result.gate_result == "passed"
    await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
        status="scored" if passed else "rejected_gate", status_reason=gate_reason(result),
        first_full_scored_at=func.coalesce(vacancies.c.first_full_scored_at, now), updated_at=now,
    ))
    return f"full:{result.category}" if passed else "full:rejected_gate"


async def _mark_error(conn, vacancy, exc, now) -> None:
    """Статус не меняется: запись повторяется при следующем запуске, причина — в status_reason и журнале."""
    error = f"{type(exc).__name__}: {exc}"[:300]
    log.warning("score_failed", extra={"vacancy_id": vacancy["id"], "error": error})
    await conn.execute(update(vacancies).where(vacancies.c.id == vacancy["id"]).values(
        status_reason=f"score_error: {error}"[:300], updated_at=now))
