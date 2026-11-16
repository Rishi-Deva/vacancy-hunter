"""RUL-1…RUL-9: правила до LLM (TEST_PLAN.md, раздел 4.4; TECHNICAL_SPEC.md, разделы 8, 12.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from vacancy_hunter.config import load_app_config
from vacancy_hunter.pipeline.prefilter import PRELIMINARY_PENDING, decide, seniority_in_role

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
RULES = load_app_config().scoring
LONG_TEXT = ("Задачи: разработка и поддержка сервисов, работа с базой данных, интеграции. "
             "Условия: удалённо, гибкий график, оплата по договорённости.")


def vac(title, text=LONG_TEXT, *, has_description=True, url="https://t.me/chan/1", **extra):
    return {"title": title, "description_text": text if has_description else None,
            "has_description": has_description, "canonical_url": url, "published_at": NOW - timedelta(days=1),
            **extra}


def d(v):
    return decide(v, RULES, NOW)


def test_rul1_accountant_rejected():
    assert (d(vac("Бухгалтер")).status, d(vac("Бухгалтер")).reason) == ("rejected_rules", "incompatible_role")


@pytest.mark.parametrize("title", ["Java-разработчик", "PHP developer", "iOS-разработчик (удалённо)",
                                   "Менеджер по продажам IT-услуг", "Frontend-разработчик React"])
def test_incompatible_roles_rejected(title):
    assert d(vac(title)).reason == "incompatible_role"


@pytest.mark.parametrize("title", ["Java/Python-разработчик", "QA-инженер (Java)", "JavaScript и Python разработчик"])
def test_rul2_incompatible_with_profile_word_passes(title):
    assert d(vac(title)).status == "pending_llm"


@pytest.mark.parametrize("title,expected", [
    ("Веб-разработчик/Дизайнер / ТЕХБЕНЗО", "pending_llm"),   # сомнение — на извлечение
    ("UX/UI-дизайнер", "rejected_rules"),
    ("Дизайнер / TG @recruiter", "rejected_rules"),          # после « / » — компания или контакт
    ("Аналитик/Бухгалтер", "pending_llm"),
])
def test_mixed_roles_only_unambiguous_rejected(title, expected):
    assert d(vac(title)).status == expected


def test_javascript_is_not_java():
    assert d(vac("JavaScript-разработчик (Node.js)")).status == "pending_llm"


def test_rul3_senior_with_description_goes_to_extraction():
    assert d(vac("Senior Python Developer")).status == "pending_llm"


def test_rul4_text_without_profile_technologies_passes():
    assert d(vac("Инженер по внедрению", "Требуется знание Kubernetes, Kafka и Go. " * 3)).status == "pending_llm"


@pytest.mark.parametrize("title,text", [("#резюме Python-разработчик", LONG_TEXT),
                                        ("Ищу работу Python-разработчиком", LONG_TEXT),
                                        ("Python-разработчик, 3 года опыта", LONG_TEXT + " #резюме #ищуработу")])
def test_rul5_resume_rejected_as_not_vacancy(title, text):
    assert d(vac(title, text)).reason == "not_vacancy"


def test_rul6_one_off_project_passes():
    text = "Разовый заказ: написать парсер сайта на Python. Бюджет 15 000 ₽, срок неделя. Без трудоустройства в штат."
    assert d(vac("Нужен парсер на Python", text)).status == "pending_llm"


def test_rul7_java_only_in_body_passes():
    assert d(vac("Python-разработчик", LONG_TEXT + " Будет плюсом знание Java и опыт продаж.")).status == "pending_llm"


def test_rul8_hh_without_description_level_word_awaits_description():
    v = vac("Ведущий тестировщик", has_description=False, url="https://hh.kz/vacancy/1")
    assert (d(v).status, d(v).reason) == ("awaiting_description", "level_word_in_title")


@pytest.mark.parametrize("title", ["Помощник руководителя проектов, Python", "Python-разработчик в главный офис",
                                   "Тестировщик (Junior)"])
def test_rul9_level_word_not_about_role_is_not_awaiting(title):
    v = vac(title, has_description=False, url="https://hh.kz/vacancy/1")
    assert (d(v).status, d(v).reason) == ("new", PRELIMINARY_PENDING)


@pytest.mark.parametrize("title,expected", [("Senior Python Developer", True), ("Lead QA", True),
                                            ("Руководитель отдела разработки", True), ("Тимлид backend", True),
                                            ("Помощник руководителя", False), ("Leading company: Python dev", False),
                                            ("Python-разработчик", False)])
def test_seniority_word_context(title, expected):
    assert seniority_in_role(title, RULES) is expected


def test_too_old_rejected():
    v = vac("Python-разработчик")
    v["published_at"] = NOW - timedelta(days=15)
    assert d(v).reason == "too_old"


def test_empty_short_post_without_link_rejected_but_with_link_passes():
    assert d(vac("Python", "Python, удалённо")).reason == "empty"
    assert d(vac("Python", "Python, удалённо", url="https://company.ru/jobs/1")).status == "pending_llm"


def test_office_only_structured_field():
    assert d(vac("Python-разработчик", work_format_src="office")).reason == "office_only"
    assert d(vac("Python-разработчик", work_format_src="remote")).status == "pending_llm"
