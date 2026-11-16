"""Объяснение оценки шаблонами из данных оценки (TECHNICAL_SPEC.md, раздел 13). LLM не участвует.

Здесь — текст карточки без разметки Telegram. Экранирование, предел длины сообщения
и кнопки добавляет отчёт (шаг A7).
"""

from __future__ import annotations

from vacancy_hunter.matching.preliminary import TITLE_ONLY_NOTE
from vacancy_hunter.matching.scoring import CRITERIA, CRITERIA_TEXT

CATEGORY_TEXT = {"excellent": "отличное соответствие", "good": "хорошее соответствие",
                 "consider": "можно рассмотреть", "hidden": "скрыто"}
OFFER_TYPE_TEXT = {"job": "Работа", "internship": "Стажировка", "project": "Разовый проект / фриланс",
                   "unknown": "Тип не определён"}
FORMAT_TEXT = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис"}
COUNTRY_TEXT = {"RU": "Россия", "KZ": "Казахстан", "BY": "Беларусь", "UZ": "Узбекистан", "KG": "Кыргызстан",
                "AM": "Армения", "AZ": "Азербайджан", "MD": "Молдова", "TJ": "Таджикистан", "TM": "Туркменистан",
                "GE": "Грузия", "UA": "Украина", "US": "США", "DE": "Германия", "CY": "Кипр", "RS": "Сербия",
                "AE": "ОАЭ", "TR": "Турция", "PL": "Польша", "IL": "Израиль", "GB": "Великобритания"}
# Сильнейшая причина выбирается из стека, задач и уровня: направление и условия — фон, а не довод.
REASON_CRITERIA = ("stack", "tasks", "level")


def _share(breakdown: dict, name: str) -> float:
    top = breakdown["max"][name]
    return breakdown[name] / top if top else 1.0


def strong_reason(name: str, explanation: dict) -> str:
    if name == "stack":
        required = explanation["skills"]["required"]
        if required and all(s["status"] == "have" for s in required):
            return "закрыты все обязательные навыки"
        return "большая часть навыков совпадает"
    if name == "tasks":
        return "задачи совпадают с проектами"
    if name == "level":
        return "требования к опыту подходят"
    if name == "direction":
        return "основное направление"
    return "условия описаны ясно"


def risk_text(name: str, explanation: dict) -> str:
    if name == "stack":
        missing = explanation["missing"][:2]
        return "не хватает навыков" + (": " + ", ".join(missing) if missing else "")
    if name == "tasks":
        return "задачи мало пересекаются с проектами"
    if name == "level":
        return explanation["level"]["basis"]
    if name == "direction":
        return "направление не основное"
    return "условия работы описаны неполно"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + ("" if text.endswith(".") else ".")


def conclusion(score: dict) -> str:
    """Вывод одной строкой (13.2)."""
    if score["kind"] == "preliminary":
        return TITLE_ONLY_NOTE
    b, e, total = score["breakdown"], score["explanation"], score["total"]
    ranked = sorted(REASON_CRITERIA, key=lambda n: -_share(b, n))          # sorted устойчив: порядок при равенстве
    weakest = sorted(CRITERIA, key=lambda n: _share(b, n))[0]
    if total >= 85:
        text = "Стоит откликнуться. " + _sentence(", ".join(strong_reason(n, e) for n in ranked[:2]))
    elif total >= 70:
        text = f"Хороший вариант. {_sentence(strong_reason(ranked[0], e))} Риск: {risk_text(weakest, e)}."
    else:
        text = f"Можно рассмотреть. Риск: {risk_text(weakest, e)}."
    if score.get("unknowns"):
        text += " Перед откликом уточнить: " + ", ".join(u["topic"] for u in score["unknowns"]) + "."
    return text


def _salary(vacancy: dict) -> str:
    lo, hi, cur = vacancy.get("salary_from"), vacancy.get("salary_to"), vacancy.get("salary_currency") or ""
    if not lo and not hi:
        return "Зарплата не указана"
    if lo and hi:
        return f"{lo:,}–{hi:,} {cur}".replace(",", " ").strip()
    return (f"от {lo:,} {cur}" if lo else f"до {hi:,} {cur}").replace(",", " ").strip()


def card_parts(vacancy: dict, score: dict, url: str | None = None, notes: list[str] | None = None) -> list[tuple[str, str]]:
    """Строки карточки по порядку раздела 13.1 с именами: сокращать можно только requirements,
    missing и matched (13.4)."""
    e, b = score["explanation"], score["breakdown"]
    # Тип берётся из вакансии (он уточняется после оценки), из оценки — только если у вакансии не определён.
    kind = vacancy.get("offer_type") if vacancy.get("offer_type") not in (None, "unknown") else e.get("offer_type")
    offer = OFFER_TYPE_TEXT.get(kind or "unknown", "Тип не определён")
    head = f"{offer} · {vacancy.get('title') or 'Без названия'}"
    if vacancy.get("company_name"):
        head += f" — {vacancy['company_name']}"
    total = score.get("display_total", score["total"])
    parts = [("head", head), ("total", f"{total} из 100 · {CATEGORY_TEXT[score['category']]}")]
    country = b["adjustments"].get("employer_country")
    published = vacancy.get("published_at") or vacancy.get("first_seen_at")
    third = [_salary(vacancy)]
    fmt = FORMAT_TEXT.get(vacancy.get("work_format_src") or "")
    if score["kind"] == "full" and score["gates"]["format"]["state"] == "yes":
        fmt = "удалённо"
    if fmt:
        third.append(fmt)
    if country:
        third.append(COUNTRY_TEXT.get(country, country))
    if published:
        third.append(published.strftime("%d.%m.%Y"))
    parts.append(("facts", " · ".join(third)))
    adj = b["adjustments"]["total"]
    if score["kind"] == "full":
        parts.append(("criteria", " · ".join(f"{CRITERIA_TEXT[n]} {b[n]}/{b['max'][n]}" for n in CRITERIA) +
                      f" · Поправки {adj:+d}"))
        if e["matched"]:
            parts.append(("matched", "Совпало: " + ", ".join(e["matched"][:6])))
        missing = "Не хватает: " + ", ".join(e["missing"][:4]) if e["missing"] else ""
        if e["unconfirmed"]:
            missing += ("; " if missing else "") + "требует подтверждения: " + ", ".join(e["unconfirmed"])
        if missing:
            parts.append(("missing", missing))
        if e["requirements"]:
            parts.append(("requirements", "Требования: " + "; ".join(e["requirements"][:3])))
    else:
        parts.append(("criteria", f"Предварительно: {b['basis']} {b['base']} · Поправки {adj:+d}"))
    checks = [f"{u['topic']} — {u['reason']}" for u in score.get("unknowns") or []] if score["kind"] == "full" else []
    checks += notes or []
    if checks:
        parts.append(("unknowns", "Требует проверки: " + "; ".join(checks)))
    text = conclusion(score)
    parts.append(("conclusion", "Вывод: " + text[:1].lower() + text[1:] if score["kind"] == "full" else text))
    if url:
        parts.append(("url", url))
    return parts


def card_lines(vacancy: dict, score: dict, url: str | None = None, notes: list[str] | None = None) -> list[str]:
    return [text for _, text in card_parts(vacancy, score, url, notes)]
