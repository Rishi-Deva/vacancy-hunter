"""Проверка ответа P1 кодом (AGENT_PROMPTS.md, разделы 3–6; ARCHITECTURE.md, 7.1).

Ответ недействителен (одна повторная попытка), если:
- не разбирается как JSON нужной формы;
- `is_offer` не проходит проверку (значение, непустая подтверждённая цитата);
- числа или размеры списков вне допустимых границ (стаж 0–20, навыков до 30 и т. д.);
- больше половины непустых цитат не найдены в тексте.
Факт с неподтверждённой цитатой, цитатой длиннее 200 знаков или пустой цитатой при
значении не `not_specified` понижается до «неизвестно» (S6). Элементы списков с
неподтверждённой цитатой отбрасываются. Ссылки, адреса почты и телефоны из
текстовых полей ответа удаляются (S7): ссылка в карточке берётся только из источника.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from vacancy_hunter.config.schemas import DIRECTION_CODES, TASK_TYPES
from vacancy_hunter.pipeline.p1 import CONFIDENCE, ENUMS, FACT_FIELDS, P1_SCHEMA

MAX_QUOTE = 200
NOT_SPECIFIED = "not_specified"
# Значение «неизвестно» для фактов, в перечне которых нет not_specified.
UNKNOWN_VALUE = {"offer_type": "unknown", "direction": NOT_SPECIFIED, "sales_share": "unclear"}
LIMITS = {"languages": 5, "legal": 10, "tasks": 10, "other_requirements": 10, "apply_instructions": 5,
          "risk_signs": 10}
URL = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\+?\d[\d ()\-]{8,}\d")
HANDLE = re.compile(r"(?<![\w])@[A-Za-z][\w]{3,31}")


class InvalidResponse(ValueError):
    pass


@dataclass
class Validated:
    facts: dict[str, Any]
    quote_check: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    text = re.sub(r"[«»“”„\"']", '"', text)
    text = re.sub(r"[‐-―−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _enum_values(schema: dict) -> list[str]:
    return schema.get("enum", [])


def _clean_text(value: str, limit: int = 300) -> str:
    """Убрать ссылки и контакты из текста ответа модели."""
    value = URL.sub("", value or "")
    value = EMAIL.sub("", value)
    value = PHONE.sub("", value)
    value = HANDLE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


class _Checker:
    def __init__(self, source_text: str):
        self.source = _norm(source_text)
        self.nonempty = 0
        self.unverified = 0
        self.downgraded: list[str] = []
        self.dropped: list[str] = []

    def quote_ok(self, quote: str, *, count: bool = True) -> bool:
        """Цитата не длиннее 200 знаков и найдена в тексте. Пустая цитата считается не подтверждённой.

        count=False — цитата не учитывается в правиле «больше половины не подтверждено».
        """
        if not quote:
            return False
        verified = len(quote) <= MAX_QUOTE and _norm(quote) in self.source
        if count:
            self.nonempty += 1
            self.unverified += 0 if verified else 1
        return verified


def _lower(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _check_fact(name: str, fact: Any, checker: _Checker, strict_offer: bool = True) -> dict:
    allowed = ENUMS[name]
    unknown = UNKNOWN_VALUE.get(name, NOT_SPECIFIED)
    if not isinstance(fact, dict):
        raise InvalidResponse(f"{name}: ожидается объект")
    value = _lower(fact.get("value"))
    quote = fact.get("quote") or ""
    confidence = _lower(fact.get("confidence"))
    if confidence not in CONFIDENCE:
        confidence = "low"
    if value not in allowed:
        if name == "is_offer" and strict_offer:
            raise InvalidResponse(f"is_offer: недопустимое значение {value!r}")
        checker.downgraded.append(name)
        return {"value": unknown, "quote": "", "confidence": "low"}
    if value == unknown or value == NOT_SPECIFIED:
        return {"value": value, "quote": "", "confidence": confidence}
    # В частях после первой общие признаки объявления часто не на что опереть: их цитаты
    # не учитываются в правиле «больше половины», но неподтверждённый признак понижается.
    whole_offer_fact = not strict_offer and name in ("is_offer", "offer_type", "direction")
    if not checker.quote_ok(quote, count=not whole_offer_fact):
        if name == "is_offer" and strict_offer:
            raise InvalidResponse("is_offer: цитата не подтверждена текстом")
        if name == "is_offer":
            # Часть длинного текста после первой может не содержать признака предложения:
            # признак берётся из любой части при объединении (AGENT_PROMPTS.md, 5.4).
            checker.downgraded.append(name)
            return {"value": "no", "quote": "", "confidence": "low"}
        checker.downgraded.append(name)
        return {"value": unknown, "quote": "", "confidence": "low"}
    return {"value": value, "quote": quote, "confidence": confidence}


def _check_list(name: str, items: Any, checker: _Checker, normalizer) -> list[dict]:
    if not isinstance(items, list):
        raise InvalidResponse(f"{name}: ожидается список")
    if len(items) > LIMITS[name]:
        raise InvalidResponse(f"{name}: больше {LIMITS[name]} элементов")
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise InvalidResponse(f"{name}: элемент не объект")
        clean = normalizer(item)
        if clean is None:
            continue
        quote = item.get("quote")
        if quote is not None and not checker.quote_ok(quote):
            checker.dropped.append(name)
            continue
        if quote is not None:
            clean["quote"] = quote
        result.append(clean)
    return result


def _enum_or(value: Any, allowed: list[str], default: str | None) -> str | None:
    value = _lower(value)
    return value if value in allowed else default


def validate_p1(raw: str, source_text: str, *, strict_offer: bool = True) -> Validated:
    """strict_offer=False — для частей длинного текста после первой."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise InvalidResponse("ответ не является JSON") from None
    if not isinstance(data, dict):
        raise InvalidResponse("ответ не является объектом")
    missing = [k for k in P1_SCHEMA["required"] if k not in data]
    if missing:
        raise InvalidResponse(f"нет полей: {', '.join(missing)}")
    checker = _Checker(source_text)
    props = P1_SCHEMA["properties"]
    facts: dict[str, Any] = {name: _check_fact(name, data[name], checker, strict_offer)
                             for name in sorted(FACT_FIELDS)}

    is_offer = facts["is_offer"]["value"] == "yes"
    kind = _enum_or(data.get("not_offer_kind"), ENUMS["not_offer_kind"], "other")
    facts["not_offer_kind"] = "none" if is_offer else (kind if kind != "none" else "other")
    facts["title"] = _clean_text(str(data.get("title") or ""), 200)
    facts["company"] = _clean_text(str(data.get("company") or ""), 200)

    exp = data["experience"]
    if not isinstance(exp, dict) or not isinstance(exp.get("years"), int) or isinstance(exp.get("years"), bool):
        raise InvalidResponse("experience.years: ожидается целое число")
    if not 0 <= exp["years"] <= 20:
        raise InvalidResponse("experience.years: вне диапазона 0–20")
    obligation = _enum_or(exp.get("obligation"), ["required", "preferred", "unclear", NOT_SPECIFIED], NOT_SPECIFIED)
    exp_conf = _enum_or(exp.get("confidence"), CONFIDENCE, "low")
    if obligation != NOT_SPECIFIED and not checker.quote_ok(exp.get("quote") or ""):
        checker.downgraded.append("experience")
        facts["experience"] = {"years": 0, "obligation": NOT_SPECIFIED, "quote": "", "confidence": "low"}
    else:
        facts["experience"] = {"years": exp["years"] if obligation != NOT_SPECIFIED else 0, "obligation": obligation,
                               "quote": (exp.get("quote") or "") if obligation != NOT_SPECIFIED else "",
                               "confidence": exp_conf}

    geo = data["geo"]
    countries = geo.get("countries") if isinstance(geo, dict) else None
    if not isinstance(countries, list) or len(countries) > 20:
        raise InvalidResponse("geo.countries: ожидается список до 20 кодов")
    geo_kind = _enum_or(geo.get("kind"), ENUMS["geo.kind"], NOT_SPECIFIED)
    codes = sorted({c.strip().upper() for c in countries if isinstance(c, str) and re.fullmatch(r"[A-Za-z]{2}", c.strip())})
    if geo_kind != NOT_SPECIFIED and not checker.quote_ok(geo.get("quote") or ""):
        checker.downgraded.append("geo")
        facts["geo"] = {"kind": NOT_SPECIFIED, "countries": [], "quote": "", "confidence": "low"}
    else:
        facts["geo"] = {"kind": geo_kind, "countries": codes if geo_kind != NOT_SPECIFIED else [],
                        "quote": (geo.get("quote") or "") if geo_kind != NOT_SPECIFIED else "",
                        "confidence": _enum_or(geo.get("confidence"), CONFIDENCE, "low")}

    ec = data["employer_country"] if isinstance(data["employer_country"], dict) else {}
    code = (ec.get("code") or "").strip().upper()
    if code and re.fullmatch(r"[A-Z]{2}", code) and checker.quote_ok(ec.get("quote") or ""):
        facts["employer_country"] = {"code": code, "quote": ec["quote"],
                                     "confidence": _enum_or(ec.get("confidence"), CONFIDENCE, "low")}
    else:
        if code:
            checker.downgraded.append("employer_country")
        facts["employer_country"] = {"code": "", "quote": "", "confidence": "low"}

    lang_levels = ENUMS["languages.level"]
    facts["languages"] = _check_list("languages", data["languages"], checker, lambda i: {
        "language": _clean_text(str(i.get("language") or ""), 40).lower(),
        "level": _enum_or(i.get("level"), lang_levels, NOT_SPECIFIED),
        "obligation": _enum_or(i.get("obligation"), ["required", "preferred", "unclear"], "unclear"),
    } if i.get("language") else None)
    legal_kinds = ENUMS["legal.kind"]
    facts["legal"] = _check_list("legal", data["legal"], checker, lambda i: {
        "kind": _enum_or(i.get("kind"), legal_kinds, "other"), "detail": _clean_text(str(i.get("detail") or ""), 200),
        "obligation": _enum_or(i.get("obligation"), ["required", "preferred", "unclear"], "unclear"),
    })
    pt = data["project_terms"] if isinstance(data["project_terms"], dict) else {}
    facts["project_terms"] = {k: _clean_text(str(pt.get(k) or ""), 200) for k in ("budget", "deadline", "payment")}

    sal = data["salary"] if isinstance(data["salary"], dict) else {}
    present = sal.get("present") is True
    a_from, a_to = sal.get("amount_from") or 0, sal.get("amount_to") or 0
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (a_from, a_to)):
        raise InvalidResponse("salary: суммы должны быть неотрицательными целыми")
    facts["salary"] = {"present": present, "amount_from": a_from if present else 0, "amount_to": a_to if present else 0,
                       "currency": (sal.get("currency") or "").strip().upper()[:3] if present else "",
                       "period": _enum_or(sal.get("period"), ["month", "hour", "project", "other"], "other")}

    for name in ("skills_required", "skills_optional"):
        block = data[name] if isinstance(data[name], dict) else {}
        items = block.get("items")
        if not isinstance(items, list) or len(items) > 30:
            raise InvalidResponse(f"{name}: ожидается список до 30 навыков")
        clean = list(dict.fromkeys(s.strip().lower() for s in items if isinstance(s, str) and s.strip()))
        quote = block.get("quote") or ""
        verified = checker.quote_ok(quote) if clean else True
        # Навыки не участвуют в жёстких фильтрах; неподтверждённая цитата отмечается, перечень сохраняется.
        facts[name] = {"items": clean, "quote": quote if verified else "", "quote_verified": verified}
    required = set(facts["skills_required"]["items"])
    facts["skills_optional"]["items"] = [s for s in facts["skills_optional"]["items"] if s not in required]

    if not isinstance(data["tasks"], list) or len(data["tasks"]) > LIMITS["tasks"]:
        raise InvalidResponse("tasks: ожидается список до 10 задач")
    facts["tasks"] = []
    for task in data["tasks"]:
        if not isinstance(task, dict):
            raise InvalidResponse("tasks: элемент не объект")
        text = _clean_text(str(task.get("text") or ""), 120)
        if text:
            facts["tasks"].append({"text": text, "type": _enum_or(task.get("type"), TASK_TYPES, "other"),
                                   "core": task.get("core") is True})

    req_kinds = ENUMS["other_requirements.kind"]
    facts["other_requirements"] = _check_list("other_requirements", data["other_requirements"], checker, lambda i: {
        "text": _clean_text(str(i.get("text") or ""), 200), "kind": _enum_or(i.get("kind"), req_kinds, "other"),
        "obligation": _enum_or(i.get("obligation"), ["required", "preferred", "unclear"], "unclear"),
    } if i.get("text") else None)
    risk_kinds = ENUMS["risk_signs.kind"]
    # Признак сомнительности — только с прямой подтверждённой цитатой.
    facts["risk_signs"] = _check_list("risk_signs", data["risk_signs"], checker, lambda i: {
        "kind": _enum_or(i.get("kind"), risk_kinds, "other")} if i.get("quote") else None)

    dl = data["apply_deadline"] if isinstance(data["apply_deadline"], dict) else {}
    date = (dl.get("date") or "").strip()
    if date and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not checker.quote_ok(dl.get("quote") or "")):
        checker.downgraded.append("apply_deadline")
        date = ""
    facts["apply_deadline"] = {"date": date, "quote": (dl.get("quote") or "") if date else ""}
    facts["apply_instructions"] = _check_list("apply_instructions", data["apply_instructions"], checker, lambda i: {
        "text": _clean_text(str(i.get("text") or ""), 300)} if i.get("text") else None)

    ai = data["ai_directed"] if isinstance(data["ai_directed"], dict) else {}
    ai_present = ai.get("present") is True
    ai_quote = ai.get("quote") or ""
    if ai_present and not checker.quote_ok(ai_quote):
        # Признак сохраняется даже без подтверждённой цитаты: он только показывается владельцу.
        checker.downgraded.append("ai_directed.quote")
        ai_quote = ""
    facts["ai_directed"] = {"present": ai_present, "quote": ai_quote if ai_present else ""}

    if checker.nonempty and checker.unverified * 2 > checker.nonempty:
        raise InvalidResponse(f"не подтверждено больше половины цитат: {checker.unverified} из {checker.nonempty}")

    quote_check = {"quotes_total": checker.nonempty, "quotes_unverified": checker.unverified,
                   "downgraded": sorted(set(checker.downgraded)), "dropped": checker.dropped}
    flags = {"not_offer": not is_offer, "ai_directed": ai_present,
             "apply_instructions": bool(facts["apply_instructions"]), "risk": bool(facts["risk_signs"])}
    if facts["direction"]["value"] not in DIRECTION_CODES:
        facts["direction"]["value"] = NOT_SPECIFIED
    return Validated(facts=facts, quote_check=quote_check, flags=flags)
