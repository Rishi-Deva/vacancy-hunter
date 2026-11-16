"""Сравнение с размеченной выборкой (TECHNICAL_SPEC.md, 24.2; TEST_PLAN.md, раздел 7).

1. `eval-sample prepare` — 40 записей из собранных системой, без оценок системы: владелец
   размечает их сам («подходит», «можно рассмотреть», «не подходит») в столбце owner_category.
   Состав: не менее 5 разовых проектов, разные источники, отклонённые правилами и фильтрами
   тоже попадают (иначе QLT-1 не проверить).
2. `eval-sample check` — сравнение категорий:
   - система: балл от 70 — «подходит», 50–69 — «можно рассмотреть», ниже 50 или исключено
     правилами или фильтрами — «не подходит»;
   - QLT-1: ни одна запись «подходит» не исключена правилами или фильтрами;
   - QLT-2: совпадение не меньше 80 % (32 из 40).
Выборка сохраняется как постоянный эталон для каждого изменения правил и промпта.
"""

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from vacancy_hunter.storage.tables import scores, vacancies

FIT, CONSIDER, NOT_FIT = "подходит", "можно рассмотреть", "не подходит"
CATEGORIES = (FIT, CONSIDER, NOT_FIT)
COLUMNS = ["id", "source", "offer_type", "title", "company", "url", "text", "owner_category"]
SAMPLE_SIZE = 40
MIN_PROJECTS = 5
QLT2_SHARE = 0.8
EXCLUDED = ("rejected_rules", "rejected_gate")


async def prepare(conn: AsyncConnection, size: int = SAMPLE_SIZE, seed: int = 20261114) -> tuple[str, list[str]]:
    """CSV для разметки и предупреждения о составе выборки."""
    rows = [dict(r) for r in (await conn.execute(
        select(vacancies.c.id, vacancies.c.primary_source_code, vacancies.c.offer_type, vacancies.c.title,
               vacancies.c.company_name, vacancies.c.canonical_url, vacancies.c.description_text, vacancies.c.status)
        .where(vacancies.c.description_text.isnot(None),
               vacancies.c.status.in_(("scored", "reported", "rejected_gate", "rejected_rules")))
    )).mappings()]
    rnd = random.Random(seed)
    rnd.shuffle(rows)
    projects = [r for r in rows if r["offer_type"] == "project"][:max(MIN_PROJECTS, 8)]
    picked = list(projects)
    rest = [r for r in rows if r not in picked]
    rest.sort(key=lambda r: (r["status"] in EXCLUDED, r["primary_source_code"]))   # сначала оценённые, по источникам
    by_source: dict[str, list[dict]] = {}
    for r in rest:
        by_source.setdefault(r["primary_source_code"], []).append(r)
    excluded = [r for r in rest if r["status"] in EXCLUDED][: size // 5]            # около 20 % отклонённых
    picked += excluded
    while len(picked) < size and any(by_source.values()):                         # по кругу по источникам
        for source in list(by_source):
            pool = [r for r in by_source[source] if r not in picked]
            by_source[source] = pool
            if pool and len(picked) < size:
                picked.append(pool.pop(0))
    warnings = []
    if len(picked) < size:
        warnings.append(f"записей с текстом пока {len(picked)} из {size}: дождитесь извлечения или возьмите меньше")
    if len(projects) < MIN_PROJECTS:
        warnings.append(f"разовых проектов {len(projects)}, нужно не меньше {MIN_PROJECTS}")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(COLUMNS)
    for r in picked[:size]:
        excerpt = " ".join((r["description_text"] or "").split())[:700]
        writer.writerow([r["id"], r["primary_source_code"], r["offer_type"], r["title"], r["company_name"] or "",
                         r["canonical_url"] or "", excerpt, ""])
    return out.getvalue(), warnings


@dataclass
class Row:
    id: int
    title: str
    owner: str
    system: str | None
    excluded: bool
    total: int | None


def system_category(status: str, total: int | None, gate_result: str | None) -> tuple[str | None, bool]:
    if status in EXCLUDED or gate_result == "rejected":
        return NOT_FIT, True
    if total is None:
        return None, False
    return (FIT if total >= 70 else CONSIDER if total >= 50 else NOT_FIT), False


def read_labels(csv_text: str) -> tuple[list[dict], list[str]]:
    """CSV после редактора таблиц: разделитель «,» или «;», возможна метка BOM."""
    text = (csv_text or "").lstrip("\ufeff")
    if not text.strip():
        return [], ["файл пуст"]
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
    errors = []
    if not rows or "id" not in rows[0] or "owner_category" not in rows[0]:
        return [], ["нет столбцов id и owner_category — файл не из eval-sample prepare"]
    seen = set()
    for r in rows:
        raw = (r.get("id") or "").strip()
        if not raw.isdigit():
            errors.append(f"строка с id «{raw}»: номер должен быть числом")
            continue
        if raw in seen:
            errors.append(f"запись {raw} повторяется")
        seen.add(raw)
        if (r.get("owner_category") or "").strip().lower() not in CATEGORIES:
            errors.append(f"запись {raw}: категория «{r.get('owner_category')}» не из {', '.join(CATEGORIES)}")
    if not errors and len(rows) != SAMPLE_SIZE:
        errors.append(f"в выборке {len(rows)} записей, нужно ровно {SAMPLE_SIZE}")
    return rows, errors


async def check(conn: AsyncConnection, csv_text: str, *, sample_size: int = SAMPLE_SIZE) -> dict:
    from vacancy_hunter.storage.tables import profile_versions, scoring_versions

    labelled, errors = read_labels(csv_text)
    if sample_size != SAMPLE_SIZE:                     # для тестов на малой выборке
        errors = [e for e in errors if not e.startswith("в выборке")]
    if errors:
        return {"errors": errors}
    ids = [int(r["id"]) for r in labelled]
    found = {r["id"]: dict(r) for r in (await conn.execute(
        select(vacancies.c.id, vacancies.c.status, vacancies.c.description_text.isnot(None).label("has_text"),
               scores.c.total, scores.c.gate_result, scores.c.state, scores.c.profile_version_id,
               scores.c.scoring_version_id)
        .outerjoin(scores, and_(scores.c.vacancy_id == vacancies.c.id, scores.c.kind == "full",
                                scores.c.state != "outdated"))
        .where(vacancies.c.id.in_(ids)))).mappings()}
    current = (await conn.execute(select(
        select(profile_versions.c.id).where(profile_versions.c.is_current).scalar_subquery(),
        select(scoring_versions.c.id).where(scoring_versions.c.is_current).scalar_subquery()))).first()
    # Выборка — постоянный эталон: сравнение имеет смысл, только если все записи на месте и
    # оценены по текущим версиям профиля и правил (TECHNICAL_SPEC.md, 24.2).
    stale = []
    for vid in ids:
        info = found.get(vid)
        if info is None:
            stale.append(f"запись {vid} удалена из базы сроком хранения")
        elif not info["has_text"] and info["status"] not in EXCLUDED:
            stale.append(f"у записи {vid} очищен текст: пересчёт по новым правилам невозможен")
        elif info["total"] is not None and (info["profile_version_id"], info["scoring_version_id"]) != tuple(current):
            stale.append(f"запись {vid} оценена по прежним версиям: дождитесь окончания пересчёта")
    rows = []
    for r in labelled:
        vid = int(r["id"])
        info = found.get(vid)
        system, excluded = system_category(info["status"], info["total"], info["gate_result"]) if info else (None, False)
        rows.append(Row(vid, r.get("title") or "", r["owner_category"].strip().lower(), system, excluded,
                        info["total"] if info else None))
    matched = sum(1 for r in rows if r.system == r.owner)
    qlt1 = [r for r in rows if r.owner == FIT and r.excluded]
    need = int(QLT2_SHARE * sample_size + 0.9999)
    not_scored = [r for r in rows if r.system is None]
    valid = not stale and not not_scored
    return {"rows": rows, "total": len(rows), "matched": matched, "need": need, "valid": valid, "stale": stale,
            "qlt1_ok": valid and not qlt1, "qlt1_failures": qlt1, "qlt2_ok": valid and matched >= need,
            "not_scored": not_scored}
