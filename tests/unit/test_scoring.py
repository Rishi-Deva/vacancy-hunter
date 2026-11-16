"""GAT-1…GAT-14, SCO-1…SCO-18, PRE-1, PRE-2 — фильтры и скоринг без базы (TEST_PLAN.md, 4.6)."""

from __future__ import annotations

import copy
import random
from datetime import timedelta

import pytest

from tests.fakes.scoring import NOW, TEXT, f, facts, load_case, profile, scoring_config, vacancy
from vacancy_hunter.matching import score_full, score_preliminary
from vacancy_hunter.matching.explain import card_lines, conclusion
from vacancy_hunter.matching.gates import NO, UNKNOWN, YES, run_gates

CONFIG = scoring_config()
RULES = CONFIG.scoring
PROFILE = profile(CONFIG)


def full(v=None, fa=None, **kw):
    return score_full(v or vacancy(), fa or facts(), PROFILE, RULES, kw.pop("now", NOW), **kw)


def gates(v=None, fa=None, text=None):
    v = v or vacancy(text or TEXT)
    return run_gates(v, fa or facts(), v["description_text"], PROFILE, RULES, NOW)


# ====================================================================== GAT-1…8: 8 фильтров × 3 состояния
T = TEXT + (" Только офис в Москве. Обязателен переезд. Требуется только чтение документации на английском. "
            "Английский язык — обязательно свободный. Английский желательно. Нужен паспорт РФ. "
            "Опыт от 6 лет обязателен. Опыт желательно. Основная работа — холодные звонки. Иногда звонки клиентам. "
            "Нужно находиться в Москве. Требуется предоплата за обучение. ")


def lang(level, obligation, quote):
    return [{"language": "english", "level": level, "obligation": obligation, "quote": quote}]


GATE_CASES = [
    # (фильтр, ожидаемое состояние, изменения фактов, изменения вакансии)
    ("format", YES, {}, {}),
    ("format", NO, {"work_format": f("office", "Только офис в Москве")}, {}),
    ("format", UNKNOWN, {"work_format": f("not_specified")}, {}),
    ("geo", YES, {}, {}),
    ("geo", NO, {"geo": {"kind": "must_be_located", "countries": ["RU"], "quote": "Нужно находиться в Москве",
                         "confidence": "high"}}, {}),
    ("geo", UNKNOWN, {"geo": {"kind": "not_specified", "countries": [], "quote": "", "confidence": "low"}}, {}),
    ("language", YES, {"languages": lang("reading", "required", "Требуется только чтение документации на английском")},
     {}),
    ("language", NO, {"languages": lang("fluent", "required", "Английский язык — обязательно свободный")}, {}),
    ("language", UNKNOWN, {"languages": lang("not_specified", "unclear", "Английский желательно")}, {}),
    ("documents", YES, {}, {}),
    ("documents", NO, {"legal": [{"kind": "citizenship", "detail": "паспорт РФ", "obligation": "required",
                                  "quote": "Нужен паспорт РФ"}]}, {"_missing_citizenship": True}),
    ("documents", UNKNOWN, {"legal": [{"kind": "citizenship", "detail": "паспорт РФ", "obligation": "required",
                                       "quote": "Нужен паспорт РФ"}]}, {}),
    ("level", YES, {"experience": {"years": 1, "obligation": "required", "quote": "Опыт от 1 года",
                                   "confidence": "high"}}, {}),
    ("level", NO, {"experience": {"years": 6, "obligation": "required", "quote": "Опыт от 6 лет обязателен",
                                  "confidence": "high"}}, {}),
    ("level", UNKNOWN, {"experience": {"years": 2, "obligation": "unclear", "quote": "Опыт желательно",
                                       "confidence": "medium"}}, {}),
    ("work_nature", YES, {"sales_share": f("partial", "Иногда звонки клиентам")}, {}),
    ("work_nature", NO, {"sales_share": f("main", "Основная работа — холодные звонки")}, {}),
    ("work_nature", UNKNOWN, {"sales_share": f("unclear", "Иногда звонки клиентам")}, {}),
    ("reliability", YES, {}, {}),
    ("reliability", NO, {"risk_signs": [{"kind": "candidate_pays", "quote": "Требуется предоплата за обучение"}]}, {}),
    ("reliability", UNKNOWN, {"tasks": []}, {"company_name": None}),
    ("actuality", YES, {}, {}),
    ("actuality", NO, {}, {"status": "closed", "closed_at": NOW}),
    ("actuality", UNKNOWN, {}, {"last_seen_at": NOW - timedelta(days=10)}),
]


@pytest.mark.parametrize("gate,state,fact_over,vac_over", GATE_CASES,
                         ids=[f"{c[0]}-{c[1]}" for c in GATE_CASES])
def test_gat_1_8_each_gate_three_states(gate, state, fact_over, vac_over):
    vac_over = dict(vac_over)
    config = CONFIG
    if vac_over.pop("_missing_citizenship", False):
        # Подтверждённо отсутствующее «гражданство РФ» — только в этом случае фильтр может сказать «нет».
        prof_data = config.profile.model_dump()
        prof_data["professional"]["confirmed_missing"].append(
            {"text": "гражданство РФ", "applies_to": "legal", "kinds": ["citizenship"], "keywords": ["РФ"]})
        from dataclasses import replace

        config = replace(config, profile=type(config.profile).model_validate(prof_data))
    result = score_full(vacancy(T, **vac_over), facts(**fact_over), profile(config), RULES, NOW)
    assert result.gates[gate]["state"] == state
    others = {n: g["state"] for n, g in result.gates.items() if n != gate}
    assert NO not in others.values(), others                     # проверяется ровно один фильтр
    if state == NO:
        assert result.gate_result == "rejected" and result.gates[gate]["reason"]
    else:
        assert result.gate_result == "passed"
    if state == UNKNOWN:
        assert result.unknowns and all(u["reason"] for u in result.unknowns)   # пометка с причиной
    assert result.total > 0                                          # баллы считаются и при исключении


def test_gat9_no_without_verified_quote_becomes_unknown():
    invented = f("office", "Только офис на Тверской")          # цитаты нет в тексте
    assert gates(fa=facts(work_format=invented))["format"].state == UNKNOWN
    low = f("office", "Только офис в Москве", "low")             # цитата есть, уверенность низкая
    assert gates(fa=facts(work_format=low), text=T)["format"].state == UNKNOWN
    exp = {"years": 7, "obligation": "required", "quote": "Опыт от 7 лет", "confidence": "high"}
    assert gates(fa=facts(experience=exp))["level"].state == UNKNOWN


def test_gat10_required_5_years_without_level_word_excluded():
    text = TEXT + " Опыт от 5 лет обязателен."
    exp = {"years": 5, "obligation": "required", "quote": "Опыт от 5 лет обязателен", "confidence": "high"}
    result = full(vacancy(text, title="Python-разработчик"), facts(experience=exp))
    assert result.gates["level"]["state"] == NO and result.gate_result == "rejected"


def test_gat11_preferred_5_years_not_excluded():
    text = TEXT + " Желательно от 5 лет опыта."
    exp = {"years": 5, "obligation": "preferred", "quote": "Желательно от 5 лет опыта", "confidence": "high"}
    result = full(vacancy(text), facts(experience=exp))
    assert result.gates["level"]["state"] == YES and result.gate_result == "passed"


@pytest.mark.parametrize("kind", ["citizenship", "work_permit", "local_phone", "local_bank", "self_employed",
                                  "sole_proprietor", "employment_contract", "other"])
def test_gat12_any_legal_requirement_unknown_with_reason(kind):
    text = TEXT + " Требование к оформлению."
    legal = [{"kind": kind, "detail": "", "obligation": "required", "quote": "Требование к оформлению"}]
    gate = gates(vacancy(text), facts(legal=legal))["documents"]
    assert gate.state == UNKNOWN and gate.reason


def test_gat13_english_text_without_language_requirements_passes():
    text = "We are looking for a Python developer. Fully remote, work from anywhere. Requirements: Python."
    fa = facts(is_offer=f("yes", "We are looking for a Python developer"), work_format=f("remote", "Fully remote"),
               geo={"kind": "anywhere", "countries": [], "quote": "work from anywhere", "confidence": "high"},
               skills_required={"items": ["python"], "quote": "Requirements: Python", "quote_verified": True})
    result = full(vacancy(text), fa)
    assert result.gates["language"]["state"] == YES


def test_gat14_project_with_performer_deposit_unreliable():
    text = TEXT + " Исполнитель вносит залог 3 000 рублей."
    fa = facts(offer_type=f("project", "Ищем Python-разработчика"),
               risk_signs=[{"kind": "performer_deposit", "quote": "Исполнитель вносит залог 3 000 рублей"}])
    result = full(vacancy(text, offer_type="project"), fa)
    assert result.gates["reliability"]["state"] == NO and result.gate_result == "rejected"


# ====================================================================== SCO-1…3: примеры 11.9
@pytest.mark.parametrize("name", ["example1", "example2", "example3"])
def test_sco_1_3_spec_examples(name):
    v, fa, exp = load_case(name)
    result = full(v, fa)
    b = result.breakdown
    assert {k: b[k] for k in ("stack", "tasks", "level", "direction", "clarity")} == \
        {k: exp[k] for k in ("stack", "tasks", "level", "direction", "clarity")}
    assert b["adjustments"]["total"] == exp["adjustments"]
    assert (result.total, result.category) == (exp["total"], exp["category"])
    assert result.gate_result == "passed"
    assert [u["topic"] for u in result.unknowns] == exp["unknown_topics"]


def test_sco2_unknown_reasons_match_spec_example():
    v, fa, _ = load_case("example2")
    reasons = {u["topic"]: u["reason"] for u in full(v, fa).unknowns}
    assert "трудовой договор" in reasons["оформление"]


def test_example1_card_and_conclusion():
    v, fa, _ = load_case("example1")
    score = full(v, fa).row()
    text = "\n".join(card_lines(v, score, "https://example.com/v/1"))
    assert "90 из 100 · отличное соответствие" in text
    assert "Стек 27/30 · Опыт 20/25 · Уровень 16/20 · Направление 15/15 · Условия 8/10 · Поправки +4" in text
    assert "Совпало: python, aiogram, postgresql, docker, fastapi" in text
    assert "Не хватает: redis" in text
    assert "Требует проверки: оформление — способ не описан" in text
    assert conclusion(score).startswith("Стоит откликнуться. Закрыты все обязательные навыки, задачи совпадают")


# ====================================================================== SCO-4…11: уровень и стек
def _exp(years, obligation="required"):
    return {"years": years, "obligation": obligation, "quote": "Опыт от 1 года", "confidence": "high"}


@pytest.mark.parametrize("years,penalty,excluded", [(1, 4, False), (2.9, 4, False), (3, 12, False),
                                                    (4.9, 12, False), (5, 0, True)])
def test_sco4_experience_penalties(years, penalty, excluded):
    text = TEXT
    result = full(vacancy(text), facts(experience=_exp(years)))
    assert result.breakdown["level"] == 20 - penalty
    assert (result.gates["level"]["state"] == NO) is excluded


def test_sco5_confirmed_senior_with_3_years_only_minus_12():
    text = TEXT + " Senior Python Developer."
    fa = facts(seniority=f("senior", "Senior Python Developer"), experience=_exp(3))
    assert full(vacancy(text, title="Senior Python Developer"), fa).breakdown["level"] == 8


def test_sco6_senior_word_in_title_but_junior_by_text_no_penalty():
    text = TEXT + " Подойдёт начинающему."
    fa = facts(seniority=f("junior", "Подойдёт начинающему"))
    assert full(vacancy(text, title="Senior Python Developer"), fa).breakdown["level"] == 20


def test_senior_word_without_description_confirms_level():
    v = vacancy("", title="Senior Python Developer", has_description=False)
    assert full(v, facts()).breakdown["level"] == 14


def test_sco7_unconfirmed_skill_zero_and_separate_note():
    result = full(fa=facts(skills_required={"items": ["python", "sqlalchemy"], "quote": "Обязательно: Python",
                                            "quote_verified": True}))
    assert result.breakdown["stack"] == 15
    assert result.explanation["unconfirmed"] == ["sqlalchemy"] and "sqlalchemy" not in result.explanation["missing"]
    text = "\n".join(card_lines(vacancy(), result.row()))
    assert "требует подтверждения: sqlalchemy" in text


def test_sco8_related_skill_half():
    result = full(fa=facts(skills_required={"items": ["django"], "quote": "Обязательно: Python",
                                            "quote_verified": True}))
    assert result.breakdown["stack"] == 15
    assert result.explanation["skills"]["required"][0]["status"] == "related"


def test_skill_synonyms_versions_and_unknown():
    p = PROFILE
    assert p.match_skill("Postgres", 0.5).status == "have"
    assert p.match_skill("Python 3.12", 0.5).status == "have"
    assert p.match_skill("MySQL", 0.5).status == "related"
    assert p.match_skill("Kafka", 0.5).status == "missing"
    assert p.match_skill("Haskell", 0.5).status == "unknown"


def test_language_skill_not_counted_in_stack():
    result = full(fa=facts(skills_required={"items": ["python", "английский язык"], "quote": "Обязательно: Python",
                                            "quote_verified": True}))
    assert result.breakdown["stack"] == 30


def test_sco9_no_skills_and_no_tasks():
    result = full(fa=facts(skills_required={"items": [], "quote": "", "quote_verified": True}, tasks=[]))
    assert result.breakdown["stack"] == 15 and result.breakdown["tasks"] == 12
    assert result.explanation["notes"] == ["навыки не указаны", "задачи не описаны"]


def test_only_optional_skills_formula():
    result = full(fa=facts(skills_required={"items": [], "quote": "", "quote_verified": True},
                           skills_optional={"items": ["python", "redis"], "quote": "Обязательно: Python",
                                            "quote_verified": True}))
    assert result.breakdown["stack"] == 23        # 15 + 15 × 0,5 = 22,5 → 23


def _req(text, kind="other"):
    return {"text": text, "kind": kind, "obligation": "required", "quote": text}


def test_sco10_requirement_without_profile_info_no_penalty_but_note():
    result = full(fa=facts(other_requirements=[_req("сертификат AWS", "certificate")]))
    assert result.breakdown["level"] == 20
    assert any("сертификат AWS" in u["reason"] for u in result.unknowns)


def test_sco11_four_unmet_requirements_capped_at_9():
    from dataclasses import replace

    data = CONFIG.profile.model_dump()
    data["professional"]["confirmed_missing"].append(
        {"text": "нет водительских прав", "applies_to": "requirement", "kinds": [], "keywords": ["права"]})
    config = replace(CONFIG, profile=type(CONFIG.profile).model_validate(data))
    reqs = [_req(f"права категории {c}") for c in "ABCD"]
    result = score_full(vacancy(), facts(other_requirements=reqs), profile(config), RULES, NOW)
    assert result.breakdown["level"] == 11 and result.explanation["level"]["unmet_penalty"] == 9
    worst = score_full(vacancy(), facts(other_requirements=reqs, experience=_exp(4)), profile(config), RULES, NOW)
    assert worst.breakdown["level"] == 0                       # 20 − 12 − 9 → не ниже 0


# ====================================================================== SCO-12…18
@pytest.mark.parametrize("days,value", [(2, 2), (3, 0), (7, 0), (8, -2), (14, -2)])
def test_sco12_freshness(days, value):
    v = vacancy(published_at=NOW - timedelta(days=days, hours=1))
    assert full(v).breakdown["adjustments"]["freshness"] == value


@pytest.mark.parametrize("country,value", [("RU", 2), ("KZ", 2), ("UZ", 1), ("DE", 0), (None, 0)])
def test_sco13_employer_country(country, value):
    assert full(vacancy(country_code=country)).breakdown["adjustments"]["geo"] == value


def test_employer_country_from_fact_when_no_structural_field():
    fa = facts(employer_country={"code": "KZ", "quote": "Компания из России", "confidence": "high"})
    assert full(vacancy(), fa).breakdown["adjustments"]["geo"] == 2


@pytest.mark.parametrize("total,category", [(49, "hidden"), (50, "consider"), (69, "consider"), (70, "good"),
                                            (84, "good"), (85, "excellent")])
def test_sco14_category_boundaries(total, category):
    from vacancy_hunter.matching.scoring import category_for

    assert category_for(total, RULES) == category


def test_sco15_total_within_bounds_on_random_inputs():
    rnd = random.Random(20261113)
    skills = ["python", "redis", "django", "kafka", "sqlalchemy", "haskell", "postgres"]
    types = ["telegram_bots", "frontend", "other", "backend_api", "sales_clients"]
    for _ in range(300):
        fa = facts(
            skills_required={"items": rnd.sample(skills, rnd.randint(0, 5)), "quote": "", "quote_verified": True},
            skills_optional={"items": rnd.sample(skills, rnd.randint(0, 3)), "quote": "", "quote_verified": True},
            tasks=[{"text": "x", "type": rnd.choice(types), "core": rnd.random() < .5} for _ in range(rnd.randint(0, 6))],
            experience=_exp(rnd.choice([0, 1, 2, 3, 4, 6]), rnd.choice(["required", "preferred", "not_specified"])),
            other_requirements=[_req("x") for _ in range(rnd.randint(0, 4))],
            work_format=f(rnd.choice(["remote", "office", "not_specified"]), "Работа удалённая"),
            direction=f(rnd.choice(["qa", "other", "support_tech", "not_specified"]), "Ищем Python-разработчика"),
        )
        v = vacancy(published_at=NOW - timedelta(days=rnd.randint(0, 30)),
                    country_code=rnd.choice(["RU", "UZ", None, "US"]))
        result = full(v, fa)
        assert 0 <= result.total <= 100
        for name, top in RULES.weights.model_dump().items():
            assert 0 <= result.breakdown[name] <= top


def test_sco16_salary_does_not_change_score():
    fa_no = facts()
    fa_yes = facts(salary={"present": True, "amount_from": 300000, "amount_to": 500000, "currency": "RUB",
                           "period": "month"})
    a = full(vacancy(), fa_no)
    b = full(vacancy(salary_from=300000, salary_to=500000, salary_currency="RUB"), fa_yes)
    assert a.total == b.total and a.breakdown == b.breakdown


def test_sco17_project_with_payment_and_deadline_gets_legal_points():
    fa = facts(offer_type=f("project", "Ищем Python-разработчика"),
               project_terms={"budget": "15 000 рублей", "deadline": "одна неделя", "payment": "50% предоплата"})
    project = full(vacancy(offer_type="project"), fa)
    assert project.breakdown["clarity_parts"]["legal"] == 2
    job = full(vacancy(), facts())
    assert project.breakdown["clarity"] == job.breakdown["clarity"] + 2      # штрафа за тип нет


def test_sco18_repeat_gives_same_result():
    v, fa, _ = load_case("example2")
    first = full(copy.deepcopy(v), copy.deepcopy(fa)).row()
    second = full(copy.deepcopy(v), copy.deepcopy(fa)).row()
    assert first == second


# ====================================================================== PRE-1, PRE-2
def pre(title, days=1, **over):
    over.setdefault("offer_type", "unknown")
    v = vacancy("", days, title=title, has_description=False, description_text=None, status="new", **over)
    return score_preliminary(v, PROFILE, RULES, NOW)


def test_pre1_entry_level_main_direction_65_plus_adjustments_cap_69():
    r = pre("Стажёр Python-разработчик", country_code="RU")          # 65 + 2 + 2 = 69
    assert r.breakdown["base"] == 65 and r.total == 69 and r.category == "consider"
    r = pre("Junior QA-инженер", days=9)                              # 65 − 2
    assert r.total == 63
    assert r.explanation["note"] == "Оценка только по заголовку, полное описание не проверено."


def test_pre1_cap_holds_even_if_adjustments_are_large():
    rules = scoring_config(adjustments__freshness__fresh=3).scoring
    v = vacancy("", 0, title="Стажёр Python", has_description=False, country_code="RU")
    assert score_preliminary(v, PROFILE, rules, NOW).total == 69


@pytest.mark.parametrize("title,base,category", [
    ("Python-разработчик", 58, "consider"),
    ("Специалист технической поддержки", 50, "consider"),
    ("Офис-менеджер", 40, "hidden"),
])
def test_pre2_main_related_other(title, base, category):
    r = pre(title, days=4)                                            # без поправок
    assert r.breakdown["base"] == base and r.total == base and r.category == category


def test_preliminary_gates_only_structural_fields():
    r = pre("Python-разработчик", work_format_src="remote")
    assert r.gates["format"]["state"] == YES
    assert r.gates["geo"]["state"] == UNKNOWN and r.gate_result == "passed"


def test_word_start_matching_bot_not_in_rabota():
    assert pre("Работа в офисе", days=4).breakdown["base"] == 40
    assert pre("Разработчик ботов", days=4).breakdown["base"] == 58


def test_preliminary_offer_type_from_title():
    assert pre("Стажировка Python").explanation["offer_type"] == "internship"
    assert pre("Python-разработчик").explanation["offer_type"] == "unknown"
    assert pre("Python-разработчик", offer_type="job").explanation["offer_type"] == "job"


# ====================================================================== замечания проверки кода
def test_not_offer_with_low_confidence_is_unknown_not_excluded():
    fa = facts(is_offer=f("no", "Ищем Python-разработчика", "low"), not_offer_kind="ad")
    assert full(fa=fa).gates["reliability"]["state"] == UNKNOWN
    fa = facts(is_offer=f("no", "Ищем Python-разработчика", "high"), not_offer_kind="ad")
    assert full(fa=fa).gates["reliability"]["state"] == NO


def test_unconfirmed_6_years_not_excluded_gets_largest_penalty():
    exp = {"years": 6, "obligation": "required", "quote": "Опыт от 6 лет", "confidence": "high"}   # цитаты нет
    result = full(fa=facts(experience=exp))
    assert result.gates["level"]["state"] == UNKNOWN and result.breakdown["level"] == 8


def test_russian_spelled_differently_is_profile_language():
    text = TEXT + " Русский язык — родной."
    langs = [{"language": "russian (native)", "level": "fluent", "obligation": "required",
              "quote": "Русский язык — родной"}]
    assert gates(vacancy(text), facts(languages=langs))["language"].state == YES


def test_geo_note_not_added_when_geo_gate_is_no():
    geo = {"kind": "must_be_located", "countries": ["RU"], "quote": "Нужно находиться в Москве", "confidence": "high"}
    result = full(vacancy(T), facts(geo=geo))
    assert result.gates["geo"]["state"] == NO
    assert "география" not in [u["topic"] for u in result.unknowns]


def test_report_window_across_midnight():
    from datetime import UTC, datetime, time

    from vacancy_hunter.pipeline.recalc import in_report_window

    # отчёт в 00:05 по Тбилиси = 20:05 UTC предыдущего дня; 23:55 по Тбилиси = 19:55 UTC
    assert in_report_window(datetime(2026, 11, 12, 19, 55, tzinfo=UTC), CONFIG, time(0, 5))
    assert not in_report_window(datetime(2026, 11, 12, 19, 45, tzinfo=UTC), CONFIG, time(0, 5))


def test_sales_unclear_without_quote_means_not_mentioned():
    assert gates(fa=facts(sales_share=f("unclear")))["work_nature"].state == YES
