"""Полная оценка по фактам извлечения (TECHNICAL_SPEC.md, раздел 11).

Порядок: жёсткие фильтры → пять независимых критериев (каждый от 0 до своего максимума) →
сумма → поправки за свежесть и географию работодателя → ограничение 0–100 → категория.
Каждый недостаток учитывается в одном критерии (11.1). Зарплата и бюджет в оценке не участвуют.
Расчёт детерминирован: те же факты, профиль, правила и момент расчёта дают тот же результат (SCO-18).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

from vacancy_hunter.config.schemas import ScoringConfig
from vacancy_hunter.matching.gates import (
    GATE_TOPIC,
    LEGAL_TEXT,
    NO,
    UNKNOWN,
    YES,
    Gate,
    legal_status,
    quote_in_text,
    run_gates,
    strong,
)
from vacancy_hunter.matching.profile import MatchProfile, SkillMatch, lang_code
from vacancy_hunter.pipeline.prefilter import seniority_in_role
from vacancy_hunter.pipeline.validate_facts import _norm

CRITERIA = ["stack", "tasks", "level", "direction", "clarity"]
CRITERIA_TEXT = {"stack": "Стек", "tasks": "Опыт", "level": "Уровень", "direction": "Направление",
                 "clarity": "Условия"}
REQUIREMENT_KIND_TEXT = {"education": "образование", "certificate": "сертификат", "domain": "отраслевой опыт",
                         "schedule": "график", "equipment": "оборудование", "other": "требование"}


def round_half_up(value: Fraction | float | int) -> int:
    if isinstance(value, Fraction):
        d = Decimal(value.numerator) / Decimal(value.denominator)
    else:
        d = Decimal(str(value))
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def category_for(total: int, rules: ScoringConfig) -> str:
    c = rules.categories
    if total >= c.excellent:
        return "excellent"
    if total >= c.good:
        return "good"
    if total >= c.consider:
        return "consider"
    return "hidden"


@dataclass
class ScoreResult:
    kind: str                       # full, preliminary
    gates: dict[str, dict]
    gate_result: str                # passed, rejected
    unknowns: list[dict]
    breakdown: dict
    total: int
    category: str
    explanation: dict = field(default_factory=dict)

    def row(self) -> dict:
        return {"kind": self.kind, "gates": self.gates, "gate_result": self.gate_result, "unknowns": self.unknowns,
                "breakdown": self.breakdown, "total": self.total, "category": self.category,
                "explanation": self.explanation}

    @property
    def rejected_by(self) -> list[str]:
        return [name for name, g in self.gates.items() if g["state"] == NO]


class _Unknowns:
    """Пометки «требует проверки»: одна на тему, первая причина сохраняется."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def add(self, topic: str, reason: str) -> None:
        self.items.setdefault(topic, reason)

    def as_list(self) -> list[dict]:
        return [{"topic": t, "reason": r} for t, r in self.items.items()]


# ---------------------------------------------------------------- поправки (11.7)
def age_days(vacancy: dict, now: datetime) -> int:
    published = vacancy.get("published_at") or vacancy.get("first_seen_at") or now
    return max(0, (now - published).days)


def adjustments(vacancy: dict, facts: dict | None, profile: MatchProfile, rules: ScoringConfig,
                now: datetime) -> dict:
    fr = rules.adjustments.freshness
    age = age_days(vacancy, now)
    freshness = fr.fresh if age <= fr.fresh_max_days else fr.normal if age <= fr.normal_max_days else fr.old
    country = (vacancy.get("country_code") or "").upper()
    if not country and facts:
        country = (facts.get("employer_country") or {}).get("code") or ""
    gp = rules.adjustments.geo_priority
    geo = gp.first if country in profile.priority_first else gp.second if country in profile.priority_second \
        else gp.other
    return {"freshness": freshness, "geo": geo, "total": freshness + geo, "age_days": age,
            "employer_country": country or None}


# ---------------------------------------------------------------- критерии
def stack_points(facts: dict, profile: MatchProfile, rules: ScoringConfig) -> tuple[int, dict]:
    w = rules.weights.stack
    s = rules.stack

    def matches(name: str) -> list[SkillMatch]:
        return [profile.match_skill(x, s.related_weight) for x in facts[name]["items"] if profile.scored_in_stack(x)]

    required, optional = matches("skills_required"), matches("skills_optional")

    def avg(ms: list[SkillMatch]) -> Fraction:
        return sum((Fraction(str(m.value)) for m in ms), Fraction(0)) / len(ms)

    req_share, opt_share = Fraction(str(s.required_share)), Fraction(str(s.optional_share))
    if required and optional:
        points = round_half_up(w * (req_share * avg(required) + opt_share * avg(optional)))
    elif required:
        points = round_half_up(w * avg(required))
    elif optional:
        base = s.only_optional_base
        points = round_half_up(base + (w - base) * avg(optional))
    else:
        points = s.no_skills_value
    detail = {"required": [m.as_dict() for m in required], "optional": [m.as_dict() for m in optional],
              "no_skills": not required and not optional}
    return min(max(points, 0), w), detail


def tasks_points(facts: dict, profile: MatchProfile, rules: ScoringConfig) -> tuple[int, dict]:
    w = rules.weights.tasks
    t = rules.tasks
    tasks = facts["tasks"]
    rows = [{"text": x["text"], "type": x["type"], "core": x["core"],
             "covered": x["type"] in profile.covered_task_types, "projects": profile.projects_for(x["type"])}
            for x in tasks]
    if not rows:
        return t.no_tasks_value, {"tasks": [], "no_tasks": True}
    weight = lambda r: t.core_weight if r["core"] else t.secondary_weight  # noqa: E731
    total = sum(weight(r) for r in rows)
    covered = sum(weight(r) for r in rows if r["covered"])
    return round_half_up(Fraction(w * covered, total)), {"tasks": rows, "no_tasks": False}


def _senior_confirmed(vacancy: dict, facts: dict, text: str, rules: ScoringConfig) -> tuple[bool, str]:
    if vacancy.get("has_description", True) and text:
        sen = facts["seniority"]
        if sen["value"] in ("senior", "lead") and strong(sen, text):
            return True, f"старший уровень по тексту: «{sen['quote']}»"
        return False, ""
    if seniority_in_role(vacancy.get("title") or "", rules):
        return True, "слово уровня в названии роли"
    return False, ""


def _requirement_status(req: dict, profile: MatchProfile) -> str:
    """missing — подтверждённо невыполнимо (раздел 5.6); ok — выполнено; check — сведений нет."""
    text = _norm(req.get("text") or "")
    for item in profile.confirmed_missing:
        if item.applies_to != "requirement" or (item.kinds and req["kind"] not in item.kinds):
            continue
        if not item.keywords or any(_norm(k) in text for k in item.keywords):
            return "missing"
    if req["kind"] == "education" and "высш" in text and "высш" in _norm(profile.education):
        return "ok"
    return "check"


def level_points(vacancy: dict, facts: dict, text: str, profile: MatchProfile, rules: ScoringConfig,
                 unknowns: _Unknowns) -> tuple[int, dict]:
    w = rules.weights.level
    lv = rules.level
    exp = facts["experience"]
    penalty, basis = 0, "штрафа за стаж нет"
    if exp["obligation"] == "required":
        years = exp["years"]
        if years >= rules.gates.exclude_required_years:
            if strong(exp, text):
                basis = f"обязательный стаж от {years:g} лет — исключение фильтром «Уровень», штраф не применяется"
            else:
                # Фильтр дал «неизвестно» (цитата не подтверждена или уверенность низкая): вакансия не
                # исключена, поэтому применяется наибольший штраф за стаж, а не полные баллы.
                penalty = max(r.penalty for r in lv.experience_penalties)
                basis = f"обязательный стаж от {years:g} лет не подтверждён цитатой — наибольший штраф"
        else:
            for rule in sorted(lv.experience_penalties, key=lambda r: -r.min_years):
                if rule.min_years <= years < rule.max_years:
                    penalty, basis = rule.penalty, f"обязательный стаж {years:g} г."
                    break
            else:
                basis = "обязательный стаж меньше года" if years else "стаж не требуется"
    elif exp["obligation"] == "not_specified":
        confirmed, why = _senior_confirmed(vacancy, facts, text, rules)
        if confirmed:
            penalty, basis = lv.senior_confirmed_penalty, f"стаж не указан, {why}"
    elif exp["obligation"] == "preferred":
        basis = "стаж только желателен"
    unmet = []
    for req in facts["other_requirements"]:
        if req["obligation"] != "required":
            continue
        status = _requirement_status(req, profile)
        if status == "missing":
            unmet.append(req["text"])
        elif status == "check":
            unknowns.add(f"{REQUIREMENT_KIND_TEXT.get(req['kind'], 'требование')}",
                         f"сведений в профиле нет: {req['text']}")
    unmet_penalty = min(lv.unmet_requirement_penalty * len(unmet), lv.unmet_requirement_cap)
    points = max(0, w - penalty - unmet_penalty)
    return points, {"penalty": penalty, "basis": basis, "unmet": unmet, "unmet_penalty": unmet_penalty}


def direction_points(facts: dict, rules: ScoringConfig) -> tuple[int, dict]:
    d = rules.direction
    code = facts["direction"]["value"]
    if code in d.main_codes:
        return d.main_points, {"code": code, "category": "main"}
    if code in d.related_codes:
        return d.related_points, {"code": code, "category": "related"}
    return d.other_points, {"code": code, "category": "other"}


def is_russian(text: str) -> bool:
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text or "")
    return bool(letters) and sum(1 for ch in letters if re.match(r"[А-Яа-яЁё]", ch)) / len(letters) >= 0.5


def clarity_points(facts: dict, text: str, gates: dict[str, Gate], profile: MatchProfile, rules: ScoringConfig,
                   offer_type: str, unknowns: _Unknowns) -> tuple[int, dict]:
    c = rules.clarity
    parts = {"remote": 0, "geo": 0, "legal": 0, "language": 0}
    if gates["format"].state == YES:
        parts["remote"] = c.remote
    if gates["geo"].state == YES:
        parts["geo"] = c.geo
    elif gates["geo"].state == UNKNOWN:
        unknowns.add("география", gates["geo"].reason or "география не указана")
    checks = [e for e in facts["legal"] if legal_status(e, profile) != "ok"]
    if offer_type == "project":
        terms = facts["project_terms"]
        described = bool(terms.get("payment")) and bool(terms.get("deadline"))
        if described and not checks:
            parts["legal"] = c.legal
        elif not described:
            unknowns.add("оформление", "порядок оплаты или срок проекта не описаны")
    elif facts["legal"] and not checks:
        parts["legal"] = c.legal
    elif not facts["legal"]:
        unknowns.add("оформление", "способ не описан")
    if checks:
        names = "; ".join(dict.fromkeys(e.get("detail") or LEGAL_TEXT[e["kind"]] for e in checks))
        unknowns.add("оформление", f"требует проверки: {names}")
    if gates["language"].state == YES:
        foreign = [i for i in facts["languages"] if lang_code(i["language"]) not in profile.languages]
        russian_stated = any(lang_code(i["language"]) == "ru" for i in facts["languages"])
        if russian_stated or (is_russian(text) and not foreign) or \
                (foreign and all(i["obligation"] == "preferred" or i["level"] == "reading" for i in foreign)):
            parts["language"] = c.language
    return sum(parts.values()), parts


# ---------------------------------------------------------------- итог
def score_full(vacancy: dict, facts: dict, profile: MatchProfile, rules: ScoringConfig, now: datetime,
               *, quote_check: dict | None = None) -> ScoreResult:
    text = vacancy.get("description_text") or ""
    gates = run_gates(vacancy, facts, text, profile, rules, now)
    unknowns = _Unknowns()
    for name, gate in gates.items():
        if gate.state == UNKNOWN:
            unknowns.add(GATE_TOPIC[name], gate.reason)
    offer_type = vacancy.get("offer_type") or facts["offer_type"]["value"]

    stack, stack_d = stack_points(facts, profile, rules)
    tasks, tasks_d = tasks_points(facts, profile, rules)
    level, level_d = level_points(vacancy, facts, text, profile, rules, unknowns)
    direction, direction_d = direction_points(facts, rules)
    clarity, clarity_d = clarity_points(facts, text, gates, profile, rules, offer_type, unknowns)
    criteria = {"stack": stack, "tasks": tasks, "level": level, "direction": direction, "clarity": clarity}
    subtotal = sum(criteria.values())
    adj = adjustments(vacancy, facts, profile, rules, now)
    total = min(100, max(0, subtotal + adj["total"]))

    notes = (["навыки не указаны"] if stack_d["no_skills"] else []) + \
        (["задачи не описаны"] if tasks_d["no_tasks"] else [])
    explanation = _explanation(facts, stack_d, tasks_d, level_d, direction_d, quote_check)
    explanation["notes"] = notes
    explanation["offer_type"] = offer_type
    weights = rules.weights.model_dump()
    breakdown = {**criteria, "max": weights, "sum": subtotal, "adjustments": adj, "clarity_parts": clarity_d}
    return ScoreResult(
        kind="full",
        gates={name: g.as_dict() for name, g in gates.items()},
        gate_result="rejected" if any(g.state == NO for g in gates.values()) else "passed",
        unknowns=unknowns.as_list(), breakdown=breakdown, total=total,
        category=category_for(total, rules), explanation=explanation,
    )


def _explanation(facts: dict, stack_d: dict, tasks_d: dict, level_d: dict, direction_d: dict,
                 quote_check: dict | None) -> dict:
    all_skills = stack_d["required"] + stack_d["optional"]
    matched = [s["name"] for s in all_skills if s["status"] in ("have", "related")]
    missing = [s["name"] for s in stack_d["required"] if s["status"] in ("missing", "unknown")] + \
              [s["name"] for s in stack_d["optional"] if s["status"] in ("missing", "unknown")]
    unconfirmed = [s["name"] for s in all_skills if s["status"] == "unconfirmed"]
    requirements = [t["text"] for t in facts["tasks"] if t["core"]][:3]
    exp = facts["experience"]
    if exp["obligation"] == "required" and exp["years"] and len(requirements) < 3:
        requirements.append(f"опыт от {exp['years']:g} г.")
    confidence = {"high": 0, "medium": 0, "low": 0}
    for name in ("is_offer", "offer_type", "direction", "seniority", "commercial_required", "work_format",
                 "relocation", "employment", "sales_share", "experience", "geo"):
        value = facts[name].get("confidence")
        if value in confidence and (facts[name].get("value", facts[name].get("obligation", facts[name].get("kind")))
                                    not in ("not_specified", "unknown", "unclear")):
            confidence[value] += 1
    return {
        "skills": {"required": stack_d["required"], "optional": stack_d["optional"]},
        "matched": matched, "missing": missing, "unconfirmed": unconfirmed,
        "requirements": requirements, "tasks": tasks_d["tasks"], "level": level_d, "direction": direction_d,
        "apply_instructions": [i["text"] for i in facts["apply_instructions"]],
        "ai_directed": facts["ai_directed"],
        "confidence": {**confidence, "downgraded": len((quote_check or {}).get("downgraded") or [])},
    }


__all__ = ["CRITERIA", "CRITERIA_TEXT", "ScoreResult", "adjustments", "category_for", "quote_in_text",
           "round_half_up", "score_full"]
