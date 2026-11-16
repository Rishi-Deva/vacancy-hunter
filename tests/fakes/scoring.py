"""Подставные факты, вакансии и тестовая конфигурация для фильтров и скоринга (TEST_PLAN.md, раздел 3)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hunter_core.config import load_yaml_model
from vacancy_hunter.config import AppConfig, load_app_config
from vacancy_hunter.config.schemas import ProfileConfig, SkillsConfig
from vacancy_hunter.matching import MatchProfile

DATA = Path(__file__).resolve().parents[1] / "data"
NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


def scoring_config(**scoring_overrides) -> AppConfig:
    """Настоящие правила и расписание, тестовые профиль и словарь (не зависят от config/profile.yaml)."""
    config = load_app_config()
    config = replace(config, profile=load_yaml_model(DATA / "profile_test.yaml", ProfileConfig),
                     skills=load_yaml_model(DATA / "skills_test.yaml", SkillsConfig))
    if scoring_overrides:
        data = config.scoring.model_dump()
        for path, value in scoring_overrides.items():
            node = data
            keys = path.split("__")
            for key in keys[:-1]:
                node = node[key]
            node[keys[-1]] = value
        config = replace(config, scoring=type(config.scoring).model_validate(data))
    return config


def profile(config: AppConfig | None = None) -> MatchProfile:
    return MatchProfile.from_config(config or scoring_config())


def f(value: str, quote: str = "", confidence: str = "high") -> dict:
    return {"value": value, "quote": quote, "confidence": confidence}


TEXT = ("Ищем Python-разработчика. Работа удалённая, из любой страны. Компания из России. "
        "Обязательно: Python. Опыт от 1 года.")


def facts(**over) -> dict:
    """Факты после проверки кодом (форма validate_p1) с нейтральными значениями."""
    data = {
        "is_offer": f("yes", "Ищем Python-разработчика"), "not_offer_kind": "none",
        "title": "Python-разработчик", "company": "",
        "offer_type": f("job", "Ищем Python-разработчика"),
        "direction": f("python_backend", "Ищем Python-разработчика"),
        "seniority": f("not_specified"),
        "experience": {"years": 0, "obligation": "not_specified", "quote": "", "confidence": "low"},
        "commercial_required": f("not_specified"),
        "work_format": f("remote", "Работа удалённая"),
        "relocation": f("not_specified"),
        "geo": {"kind": "anywhere", "countries": [], "quote": "из любой страны", "confidence": "high"},
        "employer_country": {"code": "", "quote": "", "confidence": "low"},
        "languages": [], "legal": [], "employment": f("not_specified"),
        "project_terms": {"budget": "", "deadline": "", "payment": ""},
        "salary": {"present": False, "amount_from": 0, "amount_to": 0, "currency": "", "period": "other"},
        "skills_required": {"items": ["python"], "quote": "Обязательно: Python", "quote_verified": True},
        "skills_optional": {"items": [], "quote": "", "quote_verified": True},
        "tasks": [{"text": "Разработка backend", "type": "backend_api", "core": True}],
        "sales_share": f("none"), "other_requirements": [], "risk_signs": [],
        "apply_deadline": {"date": "", "quote": ""}, "apply_instructions": [],
        "ai_directed": {"present": False, "quote": ""},
    }
    for key, value in over.items():
        data[key] = value
    return data


def vacancy(text: str = TEXT, days_ago: int = 1, **over) -> dict:
    data = {"id": 1, "title": "Python-разработчик", "company_name": "ООО «Пример»", "description_text": text,
            "has_description": True, "work_format_src": None, "country_code": None, "offer_type": "job",
            "published_at": NOW - timedelta(days=days_ago, hours=1), "first_seen_at": NOW - timedelta(days=days_ago),
            "last_seen_at": NOW - timedelta(hours=2), "alive_checked_at": None, "closed_at": None,
            "status": "extracted", "salary_from": None, "salary_to": None, "salary_currency": None}
    data.update(over)
    return data


def load_case(name: str) -> tuple[dict, dict, dict]:
    """Пример из tests/data/facts: вакансия, факты, ожидаемый результат."""
    case = json.loads((DATA / "facts" / f"{name}.json").read_text(encoding="utf-8"))
    v = vacancy(case["text"], case["days_ago"], **case.get("vacancy", {}))
    return v, copy.deepcopy(case["facts"]), case["expected"]
