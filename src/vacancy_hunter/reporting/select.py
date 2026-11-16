"""Отбор вакансий в ежедневный отчёт (TECHNICAL_SPEC.md, разделы 14.2, 15.1, 15.2).

Основной блок: статус `scored`, действующая полная оценка, фильтры пройдены, не из тихих
источников, не отклонена пользователем, правило первого показа (не показана в основном блоке
и по заголовку; первая полная оценка не старше 3 дней; публикация не старше 14 дней), балл
не ниже порога (ручная правка «свой балл» заменяет расчётный). Сортировка: балл, более поздняя
публикация, приоритет географии. Не более 3 от компании, не более заданного числа карточек.

Вакансия, уже показанная по заголовку, в основной блок не попадает при любом балле (15.4).
Блок «Только по заголовку»: предварительная оценка не ниже 50, не показана ранее в этом блоке,
публикация не старше 14 дней, не более 5 — вне лимита основного блока.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from vacancy_hunter.config import AppConfig
from vacancy_hunter.matching.scoring import category_for
from vacancy_hunter.reporting.settings import Settings
from vacancy_hunter.storage.tables import extractions, score_overrides, scores, source_state, vacancies, \
    vacancy_sources

SCORE_COLUMNS = [scores.c.id.label("score_id"), scores.c.kind, scores.c.state.label("score_state"),
                 scores.c.total, scores.c.category, scores.c.breakdown, scores.c.explanation, scores.c.gates,
                 scores.c.unknowns, scores.c.extraction_id]


def _score(row: dict) -> dict:
    return {"id": row["score_id"], "kind": row["kind"], "state": row["score_state"], "total": row["total"],
            "category": row["category"], "breakdown": row["breakdown"], "explanation": row["explanation"],
            "gates": row["gates"], "unknowns": row["unknowns"] or []}


def published(row: dict) -> datetime:
    return row["published_at"] or row["first_seen_at"]


async def _overrides(conn: AsyncConnection, ids: list[int]) -> dict[int, dict[str, dict]]:
    """Последняя активная правка каждого вида по вакансиям."""
    if not ids:
        return {}
    rows = (await conn.execute(select(score_overrides).where(score_overrides.c.vacancy_id.in_(ids),
                                                            score_overrides.c.is_active)
                               .order_by(score_overrides.c.created_at, score_overrides.c.id))).mappings().all()
    result: dict[int, dict[str, dict]] = {}
    for r in rows:
        result.setdefault(r["vacancy_id"], {})[r["kind"]] = dict(r)
    return result


async def _deadlines(conn: AsyncConnection, extraction_ids: list[int]) -> dict[int, str]:
    if not extraction_ids:
        return {}
    rows = (await conn.execute(select(extractions.c.id, extractions.c.facts["apply_deadline"]["date"].astext)
                               .where(extractions.c.id.in_(extraction_ids)))).all()
    return {i: d for i, d in rows if d}


async def _links(conn: AsyncConnection, ids: list[int]) -> dict[int, dict]:
    """Ссылка из записи источника (CRD-3) и идентификатор поста Telegram для проверки актуальности."""
    if not ids:
        return {}
    rows = (await conn.execute(
        select(vacancy_sources.c.vacancy_id, vacancy_sources.c.source_code, vacancy_sources.c.url,
               vacancy_sources.c.external_id, source_state.c.kind)
        .join(source_state, source_state.c.code == vacancy_sources.c.source_code)
        .where(vacancy_sources.c.vacancy_id.in_(ids)).order_by(vacancy_sources.c.id)
    )).mappings().all()
    result: dict[int, dict] = {}
    for r in rows:
        info = result.setdefault(r["vacancy_id"], {"url": None, "telegram": None, "sources": []})
        info["sources"].append(r["source_code"])
        if info["url"] is None and r["url"]:
            info["url"] = r["url"]
        if r["kind"] == "telegram" and info["telegram"] is None:
            info["telegram"] = r["external_id"]
    return result


async def _decorate(conn: AsyncConnection, rows: list[dict]) -> list[dict]:
    ids = [r["id"] for r in rows]
    links = await _links(conn, ids)
    deadlines = await _deadlines(conn, [r["extraction_id"] for r in rows if r["extraction_id"]])
    result = []
    for r in rows:
        info = links.get(r["id"], {"url": None, "telegram": None, "sources": []})
        primary = r["canonical_url"]
        result.append({"vacancy": {k: v for k, v in r.items() if k not in {c.name for c in SCORE_COLUMNS}},
                       "score": _score(r), "url": info["url"] or primary, "telegram": info["telegram"],
                       "sources": info["sources"], "deadline": deadlines.get(r["extraction_id"])})
    return result


async def main_candidates(conn: AsyncConnection, config: AppConfig, settings: Settings, now: datetime) -> list[dict]:
    report = config.scoring.report
    full_score = and_(scores.c.vacancy_id == vacancies.c.id, scores.c.kind == "full", scores.c.state != "outdated")
    rows = [dict(r) for r in (await conn.execute(
        select(vacancies, *SCORE_COLUMNS).join(scores, full_score).where(
            vacancies.c.status == "scored", scores.c.gate_result == "passed",
            vacancies.c.reported_at.is_(None), vacancies.c.title_only_reported_at.is_(None),
            vacancies.c.quiet_only.is_(False), vacancies.c.user_state != "rejected",
            vacancies.c.first_full_scored_at >= now - timedelta(days=report.first_show_days),
            func.coalesce(vacancies.c.published_at, vacancies.c.first_seen_at)
            >= now - timedelta(days=config.scoring.prefilter.max_age_days),
        )
    )).mappings().all()]
    overrides = await _overrides(conn, [r["id"] for r in rows])
    kept = []
    for r in rows:
        o = overrides.get(r["id"], {})
        if "force_exclude" in o:
            continue
        if "set_total" in o and o["set_total"]["new_total"] is not None:
            r["total"] = o["set_total"]["new_total"]
            r["category"] = category_for(r["total"], config.scoring)
        if r["total"] is None or r["total"] < settings.threshold:
            continue
        kept.append(r)
    kept.sort(key=lambda r: (-r["total"], -published(r).timestamp(),
                             -((r["breakdown"] or {}).get("adjustments", {}).get("geo") or 0), r["id"]))
    return await _decorate(conn, kept)


def pick_main(candidates: list[dict], max_cards: int, per_company: int) -> list[dict]:
    picked, by_company = [], {}
    for c in candidates:
        v = c["vacancy"]
        company = v.get("company_norm") or (v.get("company_name") or "").strip().lower() or None
        if company is not None:
            if by_company.get(company, 0) >= per_company:
                continue
            by_company[company] = by_company.get(company, 0) + 1
        picked.append(c)
        if len(picked) >= max_cards:
            break
    return picked


async def title_only_candidates(conn: AsyncConnection, config: AppConfig, now: datetime) -> list[dict]:
    full = scores.alias("full_score")
    has_full = exists().where(full.c.vacancy_id == vacancies.c.id, full.c.kind == "full",
                              full.c.state != "outdated")
    prelim = and_(scores.c.vacancy_id == vacancies.c.id, scores.c.kind == "preliminary",
                  scores.c.state != "outdated")
    rows = [dict(r) for r in (await conn.execute(
        select(vacancies, *SCORE_COLUMNS).join(scores, prelim).where(
            vacancies.c.status == "scored", scores.c.gate_result == "passed",
            scores.c.total >= config.scoring.categories.consider,
            vacancies.c.title_only_reported_at.is_(None), vacancies.c.reported_at.is_(None),
            vacancies.c.quiet_only.is_(False), vacancies.c.user_state != "rejected", ~has_full,
            func.coalesce(vacancies.c.published_at, vacancies.c.first_seen_at)
            >= now - timedelta(days=config.scoring.prefilter.max_age_days),
        ).order_by(scores.c.total.desc(), func.coalesce(vacancies.c.published_at, vacancies.c.first_seen_at).desc(),
                   vacancies.c.id).limit(config.scoring.report.title_only_max * 3)   # запас на замену закрытых
    )).mappings().all()]
    return await _decorate(conn, rows)
