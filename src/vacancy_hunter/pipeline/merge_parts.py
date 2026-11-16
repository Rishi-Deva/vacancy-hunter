"""Объединение результатов частей длинного текста (AGENT_PROMPTS.md, раздел 5.4).

- списки — объединение без повторов; навык, обязательный хотя бы в одной части, — обязательный;
- факт найден в одной части — берётся он; одинаков в нескольких — вариант с наибольшей уверенностью;
- значения противоречат — «неизвестно» с пометкой о противоречии и обеими цитатами;
- is_offer — yes, если хотя бы одна часть вернула yes;
- experience — при нескольких обязательных значениях берётся наибольшее число лет;
- ai_directed, risk_signs — признак из любой части сохраняется.
"""

from __future__ import annotations

from typing import Any

from vacancy_hunter.pipeline.p1 import FACT_FIELDS
from vacancy_hunter.pipeline.validate_facts import NOT_SPECIFIED, UNKNOWN_VALUE

RANK = {"high": 3, "medium": 2, "low": 1}


def _unknown(name: str) -> str:
    return UNKNOWN_VALUE.get(name, NOT_SPECIFIED)


def _merge_fact(name: str, values: list[dict], conflicts: list[dict]) -> dict:
    known = [v for v in values if v["value"] not in (_unknown(name), NOT_SPECIFIED)]
    if not known:
        return values[0]
    distinct = {v["value"] for v in known}
    if len(distinct) > 1:
        conflicts.append({"field": name, "values": sorted(distinct), "quotes": [v["quote"] for v in known]})
        return {"value": _unknown(name), "quote": "", "confidence": "low"}
    return max(known, key=lambda v: RANK.get(v["confidence"], 0))


def _union(lists: list[list[dict]], key) -> list[dict]:
    seen, result = set(), []
    for items in lists:
        for item in items:
            k = key(item)
            if k not in seen:
                seen.add(k)
                result.append(item)
    return result


def merge_parts(parts: list[dict]) -> tuple[dict, list[dict]]:
    """Объединить факты частей. Возвращает факты и перечень противоречий."""
    if len(parts) == 1:
        return parts[0], []
    conflicts: list[dict] = []
    merged: dict[str, Any] = {}
    for name in FACT_FIELDS - {"is_offer"}:
        merged[name] = _merge_fact(name, [p[name] for p in parts], conflicts)
    offers = [p["is_offer"] for p in parts if p["is_offer"]["value"] == "yes"]
    merged["is_offer"] = max(offers, key=lambda v: RANK[v["confidence"]]) if offers else parts[0]["is_offer"]
    merged["not_offer_kind"] = "none" if offers else parts[0]["not_offer_kind"]
    merged["title"] = next((p["title"] for p in parts if p["title"]), "")
    merged["company"] = next((p["company"] for p in parts if p["company"]), "")

    required_exp = [p["experience"] for p in parts if p["experience"]["obligation"] == "required"]
    if required_exp:
        merged["experience"] = max(required_exp, key=lambda e: e["years"])
    else:
        exp = [p["experience"] for p in parts if p["experience"]["obligation"] != NOT_SPECIFIED]
        merged["experience"] = max(exp, key=lambda e: RANK[e["confidence"]]) if exp else parts[0]["experience"]

    for name in ("geo", "employer_country"):
        key = "kind" if name == "geo" else "code"
        found = [p[name] for p in parts if p[name][key] not in ("", NOT_SPECIFIED)]
        distinct = {str(f[key]) for f in found}
        if len(distinct) > 1:
            conflicts.append({"field": name, "values": sorted(distinct), "quotes": [f["quote"] for f in found]})
            merged[name] = ({"kind": NOT_SPECIFIED, "countries": [], "quote": "", "confidence": "low"} if name == "geo"
                            else {"code": "", "quote": "", "confidence": "low"})
        else:
            merged[name] = max(found, key=lambda f: RANK[f["confidence"]]) if found else parts[0][name]

    required = list(dict.fromkeys(s for p in parts for s in p["skills_required"]["items"]))
    optional = [s for s in dict.fromkeys(s for p in parts for s in p["skills_optional"]["items"]) if s not in required]
    merged["skills_required"] = {"items": required[:30], "quote": next((p["skills_required"]["quote"] for p in parts
                                                                        if p["skills_required"]["quote"]), ""),
                                 "quote_verified": all(p["skills_required"]["quote_verified"] for p in parts)}
    merged["skills_optional"] = {"items": optional[:30], "quote": next((p["skills_optional"]["quote"] for p in parts
                                                                        if p["skills_optional"]["quote"]), ""),
                                 "quote_verified": all(p["skills_optional"]["quote_verified"] for p in parts)}

    merged["languages"] = _union([p["languages"] for p in parts], lambda i: (i["language"], i["level"]))
    merged["legal"] = _union([p["legal"] for p in parts], lambda i: (i["kind"], i["detail"]))
    merged["tasks"] = _union([p["tasks"] for p in parts], lambda i: i["text"].lower())[:10]
    merged["other_requirements"] = _union([p["other_requirements"] for p in parts], lambda i: i["text"].lower())
    merged["risk_signs"] = _union([p["risk_signs"] for p in parts], lambda i: (i["kind"], i["quote"]))
    merged["apply_instructions"] = _union([p["apply_instructions"] for p in parts], lambda i: i["text"].lower())
    merged["project_terms"] = {k: next((p["project_terms"][k] for p in parts if p["project_terms"][k]), "")
                               for k in ("budget", "deadline", "payment")}
    merged["salary"] = next((p["salary"] for p in parts if p["salary"]["present"]), parts[0]["salary"])
    merged["apply_deadline"] = next((p["apply_deadline"] for p in parts if p["apply_deadline"]["date"]),
                                    parts[0]["apply_deadline"])
    ai = [p["ai_directed"] for p in parts if p["ai_directed"]["present"]]
    merged["ai_directed"] = ai[0] if ai else {"present": False, "quote": ""}
    return merged, conflicts
