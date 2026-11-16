"""Восемь жёстких фильтров (TECHNICAL_SPEC.md, раздел 10; ARCHITECTURE.md, раздел 9.1).

Каждый фильтр возвращает «да» (yes), «нет» (no) или «неизвестно» (unknown) с причиной и цитатой.

- «нет» — только по факту с цитатой, найденной в тексте, и уверенностью не ниже средней
  (GAT-9: иначе «неизвестно»);
- «неизвестно» никогда не исключает: даёт пометку «требует проверки» с причиной;
- структурное поле источника используется наравне с фактом, при противоречии — «неизвестно».
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from vacancy_hunter.config.schemas import ScoringConfig
from vacancy_hunter.matching.profile import MatchProfile, lang_code
from vacancy_hunter.pipeline.validate_facts import MAX_QUOTE, _norm

YES, NO, UNKNOWN = "yes", "no", "unknown"
GATES = ["format", "geo", "language", "documents", "level", "work_nature", "reliability", "actuality"]
GATE_TITLE = {"format": "Формат", "geo": "География", "language": "Язык", "documents": "Документы",
              "level": "Уровень", "work_nature": "Характер работы", "reliability": "Достоверность",
              "actuality": "Актуальность"}
# Тема пометки «требует проверки» в карточке; документы и оформление — одна тема.
GATE_TOPIC = {"format": "формат", "geo": "география", "language": "язык", "documents": "оформление",
              "level": "стаж", "work_nature": "характер работы", "reliability": "достоверность",
              "actuality": "актуальность"}
LEGAL_TEXT = {"citizenship": "гражданство", "work_permit": "разрешение на работу", "local_phone": "местный телефон",
              "local_bank": "местный банковский счёт", "self_employed": "самозанятость", "sole_proprietor": "ИП",
              "employment_contract": "трудовой договор", "other": "иное требование к оформлению"}
# Какой неизвестный параметр профиля (раздел 5.5) отвечает за вид требования.
LEGAL_PARAM = {"citizenship": "documents", "work_permit": "documents", "local_phone": "foreign_phones",
               "local_bank": "employment_forms", "self_employed": "employment_forms",
               "sole_proprietor": "employment_forms", "employment_contract": "employment_forms",
               "other": "employment_forms"}
LEVEL_TEXT = {"reading": "чтение", "written": "письменный", "spoken": "разговорный", "fluent": "свободный",
              "not_specified": "уровень не указан"}
NOT_OFFER_TEXT = {"resume": "это резюме", "ad": "это реклама", "digest": "это подборка", "other": "не предложение работы",
                  "none": "не предложение работы"}
RISK_TEXT = {"candidate_pays": "оплата со стороны кандидата", "performer_deposit": "залог или предоплата исполнителя",
             "payment_data": "запрос платёжных данных", "income_no_duties": "обещание дохода без обязанностей",
             "other": "сомнительные условия"}
HARD_RISKS = {"candidate_pays", "performer_deposit", "payment_data"}
STRONG_CONFIDENCE = {"high", "medium"}


@dataclass(frozen=True)
class Gate:
    state: str
    reason: str = ""
    quote: str = ""

    def as_dict(self) -> dict:
        return {"state": self.state, "reason": self.reason, "quote": (self.quote or "")[:MAX_QUOTE]}


def quote_in_text(quote: str, text: str) -> bool:
    return bool(quote) and len(quote) <= MAX_QUOTE and _norm(quote) in _norm(text)


def strong(fact: dict, text: str) -> bool:
    """Факт годится для «нет»: цитата найдена в тексте, уверенность высокая или средняя."""
    return fact.get("confidence") in STRONG_CONFIDENCE and quote_in_text(fact.get("quote") or "", text)


def _no_or_unknown(fact: dict, text: str, reason: str) -> Gate:
    if strong(fact, text):
        return Gate(NO, reason, fact.get("quote") or "")
    return Gate(UNKNOWN, f"{reason} — без подтверждённой цитаты или с низкой уверенностью")


# ---------------------------------------------------------------- фильтры
def gate_format(vacancy: dict, facts: dict, text: str) -> Gate:
    src = (vacancy.get("work_format_src") or "").lower() or None
    wf, reloc = facts["work_format"], facts["relocation"]
    value = wf["value"]
    if reloc["value"] == "required":
        return _no_or_unknown(reloc, text, "обязательный переезд")
    if value in ("office", "hybrid"):
        if src == "remote":
            return Gate(UNKNOWN, "противоречие: в источнике удалённо, в тексте офис или гибрид")
        return _no_or_unknown(wf, text, "только офис" if value == "office" else "обязательный гибрид")
    if value == "remote":
        if src in ("office", "hybrid"):
            return Gate(UNKNOWN, "противоречие: в тексте удалённо, в источнике офис или гибрид")
        return Gate(YES, "удалённая работа", wf.get("quote") or "")
    if src == "remote":
        return Gate(YES, "удалённая работа по данным источника")
    if src in ("office", "hybrid"):
        return Gate(NO, "офис или гибрид по данным источника")
    return Gate(UNKNOWN, "формат не указан")


def gate_geo(facts: dict, text: str, profile: MatchProfile, rules: ScoringConfig) -> Gate:
    geo = facts["geo"]
    kind, countries, quote = geo["kind"], set(geo.get("countries") or []), geo.get("quote") or ""
    if kind == "anywhere":
        return Gate(YES, "работа из любой страны", quote)
    if kind in ("country_list", "must_be_located") and profile.location in countries:
        return Gate(YES, "перечень включает Грузию", quote)
    if kind == "country_list":
        q = _norm(quote)
        if any(_norm(w) in q for w in rules.gates.geo_allow_words):
            return Gate(YES, "прямо разрешены СНГ или зарубежье", quote)
        return Gate(UNKNOWN, "перечень стран без Грузии: " + (", ".join(sorted(countries)) or "не распознан"))
    if kind == "must_be_located":
        if not countries:
            return Gate(UNKNOWN, "требуется находиться в городе или стране, страна не распознана")
        return _no_or_unknown(geo, text, "нужно находиться в стране: " + ", ".join(sorted(countries)))
    return Gate(UNKNOWN, "география не указана")


def gate_language(facts: dict, text: str, profile: MatchProfile) -> Gate:
    foreign = [item for item in facts["languages"] if lang_code(item["language"]) not in profile.languages]
    if not foreign:
        return Gate(YES, "языковых требований нет" if not facts["languages"] else "требуется только русский")
    blocking = [i for i in foreign if i["obligation"] == "required" and i["level"] in ("spoken", "fluent", "written")]
    for item in blocking:
        reason = f"обязательно: {item['language']}, {LEVEL_TEXT[item['level']]}"
        if quote_in_text(item.get("quote") or "", text):
            return Gate(NO, reason, item["quote"])
        return Gate(UNKNOWN, reason + " — без подтверждённой цитаты")
    unclear = [i for i in foreign if i["obligation"] == "unclear" or
               (i["obligation"] == "required" and i["level"] == "not_specified")]
    if unclear:
        return Gate(UNKNOWN, f"язык упомянут без уровня или ясной обязательности: {unclear[0]['language']}",
                    unclear[0].get("quote") or "")
    return Gate(YES, "иностранный язык желателен или нужно только чтение")


def _missing_matches(item: MissingItem, kind: str, detail: str) -> bool:
    if item.kinds and kind not in item.kinds:
        return False
    return not item.keywords or any(_norm(k) in _norm(detail) for k in item.keywords)


def legal_status(entry: dict, profile: MatchProfile) -> str:
    """Требование к оформлению: missing — подтверждённо невыполнимо; ok — выполнимо; check — неизвестно."""
    for item in profile.confirmed_missing:
        if item.applies_to == "legal" and _missing_matches(item, entry["kind"], entry.get("detail") or ""):
            return "missing"
    param = profile.unknown_params.get(LEGAL_PARAM.get(entry["kind"], "employment_forms"))
    if isinstance(param, list) and entry["kind"] in param:
        return "ok"
    return "check"


def gate_documents(facts: dict, text: str, profile: MatchProfile) -> Gate:
    legal = facts["legal"]
    if not legal:
        return Gate(YES, "требований к документам нет")
    for entry in legal:
        if entry["obligation"] == "required" and legal_status(entry, profile) == "missing" and \
                quote_in_text(entry.get("quote") or "", text):
            return Gate(NO, f"обязательно: {LEGAL_TEXT[entry['kind']]}", entry["quote"])
    checks = [e for e in legal if legal_status(e, profile) != "ok"]
    if checks:
        names = "; ".join(dict.fromkeys(e.get("detail") or LEGAL_TEXT[e["kind"]] for e in checks))
        return Gate(UNKNOWN, f"требует проверки: {names}", checks[0].get("quote") or "")
    return Gate(YES, "требования к оформлению выполнимы")


def gate_level(facts: dict, text: str, rules: ScoringConfig) -> Gate:
    exp = facts["experience"]
    limit = rules.gates.exclude_required_years
    if exp["obligation"] == "required":
        if exp["years"] >= limit:
            return _no_or_unknown(exp, text, f"обязательный стаж от {exp['years']:g} лет")
        return Gate(YES, f"обязательный стаж {exp['years']:g} г." if exp["years"] else "стаж не требуется",
                    exp.get("quote") or "")
    if exp["obligation"] == "unclear":
        return Gate(UNKNOWN, "стаж упомянут, обязательность неясна", exp.get("quote") or "")
    if exp["obligation"] == "preferred":
        return Gate(YES, "стаж только желателен", exp.get("quote") or "")
    return Gate(YES, "стаж не указан")


def gate_work_nature(facts: dict, text: str) -> Gate:
    share = facts["sales_share"]
    if share["value"] == "main":
        return _no_or_unknown(share, text, "продажи и переговоры — основная часть работы")
    if share["value"] == "unclear":
        # «Неясно» без подтверждённой цитаты — так проверка кодом помечает отсутствие сведений:
        # в тексте нет продаж, о которых можно спросить. «Неизвестно» — только если они упомянуты.
        if quote_in_text(share.get("quote") or "", text):
            return Gate(UNKNOWN, "есть продажи или переговоры, доля неясна", share["quote"])
        return Gate(YES, "продажи не упомянуты")
    return Gate(YES, "продажи не основная часть работы" if share["value"] == "partial" else "продаж нет")


def gate_reliability(vacancy: dict, facts: dict, text: str) -> Gate:
    if facts["is_offer"]["value"] != "yes":
        reason = NOT_OFFER_TEXT.get(facts.get("not_offer_kind") or "other", "не предложение работы")
        return _no_or_unknown(facts["is_offer"], text, reason)
    for risk in facts["risk_signs"]:
        if risk["kind"] in HARD_RISKS:
            return Gate(NO, RISK_TEXT[risk["kind"]], risk.get("quote") or "")
    soft = [r for r in facts["risk_signs"] if r["kind"] not in HARD_RISKS]
    if soft:
        return Gate(UNKNOWN, RISK_TEXT[soft[0]["kind"]], soft[0].get("quote") or "")
    company = (vacancy.get("company_name") or facts.get("company") or "").strip()
    if not company and not facts["tasks"]:
        return Gate(UNKNOWN, "нет названия компании и описания обязанностей")
    return Gate(YES, "признаков сомнительности нет")


def gate_actuality(vacancy: dict, facts: dict | None, rules: ScoringConfig, now: datetime) -> Gate:
    if vacancy.get("status") == "closed" or vacancy.get("closed_at"):
        return Gate(NO, "источник сообщил о закрытии")
    deadline = ((facts or {}).get("apply_deadline") or {}).get("date") or ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
        try:
            if date.fromisoformat(deadline) < now.date():
                return Gate(NO, f"срок приёма откликов истёк {deadline}", facts["apply_deadline"].get("quote") or "")
        except ValueError:
            pass
    seen = [t for t in (vacancy.get("last_seen_at"), vacancy.get("alive_checked_at")) if t is not None]
    if seen and now - max(seen) <= timedelta(days=rules.gates.actual_seen_days):
        return Gate(YES, "вакансия видна в источнике")
    return Gate(UNKNOWN, "актуальность не проверена")


def run_gates(vacancy: dict, facts: dict, text: str, profile: MatchProfile, rules: ScoringConfig,
              now: datetime) -> dict[str, Gate]:
    return {
        "format": gate_format(vacancy, facts, text),
        "geo": gate_geo(facts, text, profile, rules),
        "language": gate_language(facts, text, profile),
        "documents": gate_documents(facts, text, profile),
        "level": gate_level(facts, text, rules),
        "work_nature": gate_work_nature(facts, text),
        "reliability": gate_reliability(vacancy, facts, text),
        "actuality": gate_actuality(vacancy, facts, rules, now),
    }
