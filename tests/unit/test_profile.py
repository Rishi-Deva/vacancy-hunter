"""Профиль и словарь навыков: только то, что есть в резюме (вымышленный пример; TECHNICAL_SPEC.md, разделы 5–6)."""

from __future__ import annotations

from vacancy_hunter.config import load_app_config
from vacancy_hunter.matching import MatchProfile

# Раздел 5.2 в канонических названиях словаря. Новый навык попадает сюда только вместе с резюме.
RESUME_SKILLS = {
    "python", "asyncio",
    "aiogram", "telegram bot api", "telegram payments", "telethon",
    "claude api", "openai api", "llm", "prompt engineering", "structured output", "llm caching", "vision", "rag",
    "fastapi", "rest api", "jwt", "api integration", "webhooks", "requests",
    "sql", "postgresql", "sqlite",
    "pypdf", "python-docx", "pandas", "openpyxl", "excel", "csv", "opencv", "barcode scanning",
    "web scraping", "devtools", "curl_cffi", "proxies", "apify",
    "pytest", "unittest", "manual testing", "test design", "bug reports", "postman", "swagger", "api testing",
    "linux", "systemd", "selinux", "ssh", "git", "github", "docker", "oracle cloud", "apscheduler", "monitoring",
    "deployment",
    "mvp planning", "technical documentation",
}
COVERED_TASKS = {  # левый столбец раздела 9.2
    "telegram_bots", "backend_api", "api_integration", "gov_api_integration", "llm_integration", "rag", "scraping",
    "internal_api_research", "documents_processing", "database_sql", "auth", "payments", "crm_features",
    "schedulers", "manual_testing", "api_testing", "python_autotests", "deploy_linux", "monitoring",
    "computer_vision", "tech_docs",
}
# Смежные пары раздела 6: требуется → засчитывается при наличии.
RELATED_PAIRS = {
    "django": "fastapi", "flask": "fastapi", "aiohttp": "fastapi", "mysql": "postgresql", "mariadb": "postgresql",
    "aiogram 2": "aiogram", "python-telegram-bot": "aiogram", "pytelegrambotapi": "aiogram",
    "scrapy": "web scraping", "beautifulsoup": "web scraping", "gitlab": "git", "bitbucket": "git",
    "insomnia": "postman", "yandexgpt": "claude api", "gigachat": "openai api", "langchain": "rag",
    "llamaindex": "rag",
}


def test_profile_has_only_resume_skills():
    profile = load_app_config().profile.professional
    confirmed = {s for group in profile.skills_confirmed.values() for s in group}
    assert confirmed == RESUME_SKILLS
    assert profile.skills_unconfirmed == ["sqlalchemy"]
    assert [lang.language for lang in profile.languages] == ["ru"]


def test_profile_projects_and_task_types():
    profile = load_app_config().profile.professional
    assert len(profile.projects) == 8
    assert set(profile.covered_task_types) == COVERED_TASKS
    used = {t for p in profile.projects for t in p.task_types}
    assert used <= COVERED_TASKS


def test_unknown_params_and_missing_as_in_spec():
    profile = load_app_config().profile.professional
    assert profile.unknown_params.model_dump() == {"documents": "unknown", "foreign_phones": "unknown",
                                                   "employment_forms": "unknown"}
    assert [m.text for m in profile.confirmed_missing] == ["рабочие языки, кроме русского"]


def test_skills_dictionary_related_pairs_and_out_of_profile():
    config = load_app_config()
    match = MatchProfile.from_config(config)
    for required, have in RELATED_PAIRS.items():
        result = match.match_skill(required, 0.5)
        assert result.status == "related", required
        assert have in config.skills.skills[[s.canonical for s in config.skills.skills].index(required)].related
    for name in ("kubernetes", "kafka", "selenium", "playwright", "jira"):
        assert match.match_skill(name, 0.5).status == "missing", name
    assert match.match_skill("SQLAlchemy", 0.5).status == "unconfirmed"


def test_spec_synonym_examples():
    match = MatchProfile.from_config(load_app_config())
    for spelling in ("Postgres", "PostgreSQL", "Постгрес"):
        assert match.canonical(spelling) == "postgresql"
