"""Готовые оценённые вакансии для тестов отчёта и интерфейса (без LLM)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

from hunter_core.storage.tables import source_state
from tests.fakes.scoring import load_case, profile, scoring_config
from vacancy_hunter.matching import score_full, score_preliminary
from vacancy_hunter.matching.scoring import category_for
from vacancy_hunter.matching.versions import ensure_versions
from vacancy_hunter.reporting.actuality import Check
from vacancy_hunter.storage.tables import scores, vacancies, vacancy_sources

NOW = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)        # 08:00 по Тбилиси
CONFIG = scoring_config()
_v, _facts, _ = load_case("example1")
FULL = score_full(_v, _facts, profile(CONFIG), CONFIG.scoring, _v["published_at"] + timedelta(days=1)).row()
PRELIM = score_preliminary({"title": "Стажёр Python", "has_description": False, "offer_type": "unknown",
                            "published_at": NOW, "first_seen_at": NOW, "last_seen_at": NOW},
                           profile(CONFIG), CONFIG.scoring, NOW).row()


class FakeChecker:
    """Проверка актуальности: заданные исходы по номеру вакансии, по умолчанию «не поддерживается»."""

    def __init__(self, results: dict[int, Check] | None = None):
        self.results = results or {}
        self.checked: list[int] = []

    async def check(self, candidate: dict, today) -> Check:
        vid = candidate["vacancy"]["id"]
        self.checked.append(vid)
        return self.results.get(vid, Check("unsupported"))


_counter = {"n": 0}


async def add_scored(engine, *, total: int = 80, company: str | None = None, offer_type: str = "job",
                     scored_ago: timedelta = timedelta(hours=2), published_ago: timedelta = timedelta(days=1),
                     status: str = "scored", quiet: bool = False, kind: str = "full", gate: str = "passed",
                     title: str | None = None, source: str = "habr_career", url: str | None = None,
                     **vacancy_over) -> int:
    _counter["n"] += 1
    n = _counter["n"]
    versions = await ensure_versions(engine, CONFIG)
    row = copy.deepcopy(FULL if kind == "full" else PRELIM)
    row.update(total=total, category=category_for(total, CONFIG.scoring), gate_result=gate)
    if kind == "preliminary" and row["category"] in ("excellent", "good"):
        row["category"] = "consider"
    async with engine.begin() as conn:
        await conn.execute(pg_insert(source_state).values(code=source, kind="telegram" if source.startswith("tg")
                                                          else "rss", mode="active").on_conflict_do_nothing())
        values = dict(title=title or f"Python-разработчик {n}", company_name=company or f"Компания {n}",
                      company_norm=(company or f"компания {n}").lower(), status=status, has_description=kind == "full",
                      offer_type=offer_type, primary_source_code=source, quiet_only=quiet,
                      first_seen_at=NOW - published_ago, last_seen_at=NOW, published_at=NOW - published_ago,
                      first_full_scored_at=NOW - scored_ago if kind == "full" else None,
                      canonical_url=f"https://example.com/v/{n}",
                      description_text=f"Текст вакансии {n}" if kind == "full" else None)
        values.update(vacancy_over)
        vid = (await conn.execute(insert(vacancies).values(**values).returning(vacancies.c.id))).scalar_one()
        await conn.execute(insert(vacancy_sources).values(
            vacancy_id=vid, source_code=source, external_id=f"ch/{n}" if source.startswith("tg") else str(n),
            url=url or f"https://source.example/{n}", first_seen_at=NOW - published_ago))
        await conn.execute(insert(scores).values(vacancy_id=vid, state="current", profile_version_id=versions.profile_id,
                                                 scoring_version_id=versions.scoring_id, created_at=NOW, **row))
    return vid


__all__ = ["CONFIG", "NOW", "FakeChecker", "add_scored", "UTC"]
