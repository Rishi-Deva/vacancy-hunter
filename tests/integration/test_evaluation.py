"""eval-sample: подготовка выборки без оценок системы и критерии QLT-1, QLT-2 (TECHNICAL_SPEC.md, 24.2)."""

from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy import update

from tests.fakes.report import add_scored
from vacancy_hunter import evaluation
from vacancy_hunter.storage.tables import vacancies

pytestmark = pytest.mark.integration


async def test_prepare_has_no_system_scores_and_includes_projects_and_rejected(aengine):
    ids = [await add_scored(aengine, total=60 + i, description_text=f"Текст {i}") for i in range(30)]
    for i in range(6):
        ids.append(await add_scored(aengine, total=55, offer_type="project", description_text=f"Заказ {i}"))
    for i in range(8):
        ids.append(await add_scored(aengine, total=40, status="rejected_gate", description_text=f"Отклонена {i}"))
    async with aengine.connect() as conn:
        text, warnings = await evaluation.prepare(conn)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 40 and warnings == []
    assert list(rows[0]) == evaluation.COLUMNS                       # нет столбцов с баллом или категорией
    assert all(r["owner_category"] == "" for r in rows)
    assert sum(r["offer_type"] == "project" for r in rows) >= 5
    assert any(r["text"].startswith("Отклонена") for r in rows)


def _csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(evaluation.COLUMNS)
    for vid, cat in rows:
        w.writerow([vid, "s", "job", f"Вакансия {vid}", "", "", "текст", cat])
    return out.getvalue()


async def test_check_qlt1_and_qlt2(aengine):
    good = [await add_scored(aengine, total=80) for _ in range(33)]
    consider = await add_scored(aengine, total=60)
    rejected = await add_scored(aengine, total=85)
    async with aengine.begin() as conn:
        await conn.execute(update(vacancies).where(vacancies.c.id == rejected).values(status="rejected_gate"))
    low = [await add_scored(aengine, total=30) for _ in range(5)]
    labelled = [(v, "подходит") for v in good] + [(consider, "можно рассмотреть"), (rejected, "подходит")] + \
               [(v, "не подходит") for v in low]
    async with aengine.connect() as conn:
        result = await evaluation.check(conn, _csv(labelled))
    assert result["total"] == 40 and result["matched"] == 39 and result["need"] == 32 and result["qlt2_ok"]
    assert not result["qlt1_ok"] and [r.id for r in result["qlt1_failures"]] == [rejected]
    async with aengine.connect() as conn:
        bad = await evaluation.check(conn, _csv([(good[0], "отлично")]))
        empty = await evaluation.check(conn, "")
        short = await evaluation.check(conn, _csv(labelled[:10]))
        twice = await evaluation.check(conn, _csv(labelled[:39] + labelled[:1]))
        semicolon = await evaluation.check(conn, "\ufeff" + _csv(labelled).replace(",", ";"))
    assert "errors" in bad and "errors" in empty
    assert short["errors"] == ["в выборке 10 записей, нужно ровно 40"]
    assert any("повторяется" in e for e in twice["errors"])
    assert semicolon["matched"] == 39


async def test_check_invalid_when_texts_purged_or_scores_outdated(aengine):
    ids = [await add_scored(aengine, total=80) for _ in range(40)]
    async with aengine.begin() as conn:
        await conn.execute(update(vacancies).where(vacancies.c.id == ids[0]).values(description_text=None))
    async with aengine.connect() as conn:
        result = await evaluation.check(conn, _csv([(v, "подходит") for v in ids]))
    assert not result["valid"] and not result["qlt2_ok"] and "очищен текст" in result["stale"][0]
