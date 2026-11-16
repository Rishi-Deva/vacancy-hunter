"""Предварительная оценка по заголовку (TECHNICAL_SPEC.md, раздел 12).

LLM не вызывается. Направление определяется по названию должности перечнями слов из
scoring.yaml; слово сравнивается с началом слов названия («бот» находит «ботов», но не «работа»).
Базовое значение: 65 — основное направление с указанием на начальный уровень; 58 — основное;
50 — смежное; 40 — не определено (не показывается). Плюс поправки раздела 11.7, потолок 69.
Жёсткие фильтры работают только по структурным полям ленты, остальные — «неизвестно».
"""

from __future__ import annotations

import re
from datetime import datetime

from vacancy_hunter.config.schemas import ScoringConfig
from vacancy_hunter.matching.gates import GATE_TOPIC, GATES, NO, UNKNOWN, YES, Gate, gate_actuality
from vacancy_hunter.matching.profile import MatchProfile
from vacancy_hunter.matching.scoring import ScoreResult, adjustments, category_for

TITLE_ONLY_NOTE = "Оценка только по заголовку, полное описание не проверено."
INTERNSHIP_WORDS = ("стажер", "стажёр", "стажировка", "intern", "internship", "trainee")
PROJECT_WORDS = ("фриланс", "freelance", "разовый", "заказ", "проект на", "подработка на проект")


def _norm_title(title: str) -> str:
    return " " + re.sub(r"[^\w+#]+", " ", (title or "").casefold().replace("ё", "е")) + " "


def has_word(title_norm: str, word: str) -> bool:
    """Слово или фраза в начале слова названия."""
    w = re.sub(r"[^\w+#]+", " ", word.casefold().replace("ё", "е")).strip()
    return bool(w) and re.search(r"(?<=\s)" + re.escape(w), title_norm) is not None


def title_direction(title: str, rules: ScoringConfig) -> str:
    t = _norm_title(title)
    p = rules.preliminary
    if any(has_word(t, w) for w in p.main_keywords):
        return "main"
    if any(has_word(t, w) for w in p.related_keywords):
        return "related"
    return "other"


def title_offer_type(title: str) -> str:
    t = _norm_title(title)
    if any(has_word(t, w) for w in INTERNSHIP_WORDS):
        return "internship"
    if any(has_word(t, w) for w in PROJECT_WORDS):
        return "project"
    return "unknown"


def score_preliminary(vacancy: dict, profile: MatchProfile, rules: ScoringConfig, now: datetime) -> ScoreResult:
    title = vacancy.get("title") or ""
    p = rules.preliminary
    direction = title_direction(title, rules)
    entry = any(has_word(_norm_title(title), w) for w in p.entry_markers)
    if direction == "main" and entry:
        base, basis = p.main_entry, "основное направление, начальный уровень"
    elif direction == "main":
        base, basis = p.main, "основное направление"
    elif direction == "related":
        base, basis = p.related, "смежное направление"
    else:
        base, basis = p.other, "направление по названию не определено"

    src = (vacancy.get("work_format_src") or "").lower()
    gates: dict[str, Gate] = {name: Gate(UNKNOWN, "только заголовок: полное описание не проверено") for name in GATES}
    if src == "remote":
        gates["format"] = Gate(YES, "удалённая работа по данным источника")
    elif src in ("office", "hybrid"):
        gates["format"] = Gate(NO, "офис или гибрид по данным источника")
    gates["actuality"] = gate_actuality(vacancy, None, rules, now)

    adj = adjustments(vacancy, None, profile, rules, now)
    total = max(0, min(p.cap, base + adj["total"]))
    category = category_for(total, rules)
    if category in ("excellent", "good"):        # потолок 69 ниже границы good; защита от правки правил
        category = "consider"
    unknowns = [{"topic": GATE_TOPIC[n], "reason": g.reason} for n, g in gates.items() if g.state == UNKNOWN]
    offer_type = vacancy.get("offer_type") if vacancy.get("offer_type") not in (None, "unknown") \
        else title_offer_type(title)
    return ScoreResult(
        kind="preliminary",
        gates={n: g.as_dict() for n, g in gates.items()},
        gate_result="rejected" if any(g.state == NO for g in gates.values()) else "passed",
        unknowns=unknowns,
        breakdown={"base": base, "basis": basis, "direction": direction, "entry_level": entry, "cap": p.cap,
                   "adjustments": adj},
        total=total, category=category,
        explanation={"note": TITLE_ONLY_NOTE, "offer_type": offer_type},
    )
