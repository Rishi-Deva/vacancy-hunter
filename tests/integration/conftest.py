"""Интеграционные тесты с настоящим PostgreSQL.

- `test_engine` — отдельная тестовая база (DB_TEST_*). Перед набором её схема
  очищается и миграции применяются заново, рабочие данные не затрагиваются.
- `app_engine` — рабочая база под пользователем приложения (DB_APP_*). Используется
  только для проверки прав: изменения выполняются в транзакциях с откатом.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, text

from hunter_core.config import SecretsError, read_database_settings
from hunter_core.storage import create_db_engine

ROOT = Path(__file__).resolve().parents[2]


def _settings(prefix: str, name_var: str):
    try:
        return read_database_settings(
            os.environ, user_var=f"{prefix}_USER", password_var=f"{prefix}_PASSWORD", name_var=name_var
        )
    except SecretsError as exc:
        pytest.fail(f"Интеграционным тестам нужна база: {exc}. Запускайте тесты в контейнере app.")


def alembic_config(url) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    cfg.attributes["skip_logging_setup"] = True
    return cfg


def _guard_against_production(test, env=os.environ) -> None:
    """Тесты очищают свою базу. Убедиться, что это точно не рабочая база и не рабочий пользователь."""
    problems = []
    if test.name == env.get("DB_NAME", "").strip():
        problems.append("DB_TEST_NAME совпадает с DB_NAME")
    if test.user in {env.get("DB_APP_USER", "").strip(), env.get("DB_MIGRATOR_USER", "").strip()}:
        problems.append("DB_TEST_USER совпадает с пользователем приложения или миграций")
    if problems:
        pytest.exit("Тесты остановлены, чтобы не стереть рабочую базу: " + "; ".join(problems), returncode=2)


@pytest.fixture(scope="session")
def test_settings():
    settings = _settings("DB_TEST", "DB_TEST_NAME")
    _guard_against_production(settings)
    return settings


def reset_schema(engine: Engine) -> None:
    """Сделать тестовую базу пустой: удалить схему со всеми таблицами и создать заново."""
    expected = os.environ.get("DB_TEST_NAME", "").strip()
    production = os.environ.get("DB_NAME", "").strip()
    with engine.begin() as conn:
        actual = conn.execute(text("SELECT current_database()")).scalar()
        if not expected or actual != expected or actual == production:
            pytest.exit(f"Отказ очищать базу «{actual}»: это не тестовая база", returncode=2)
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture(scope="session")
def test_engine(test_settings) -> Engine:
    engine = create_db_engine(test_settings, application_name="pytest")
    reset_schema(engine)
    command.upgrade(alembic_config(test_settings.url()), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def conn(test_engine):
    """Соединение с транзакцией, которая откатывается после теста: тесты не влияют друг на друга."""
    with test_engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


@pytest.fixture(scope="session")
def app_engine() -> Engine:
    engine = create_db_engine(_settings("DB_APP", "DB_NAME"), application_name="pytest-privileges")
    yield engine
    engine.dispose()


ALL_APP_TABLES_SQL = (
    "TRUNCATE outbox, report_items, reports, feedback_events, cover_letters, score_overrides, scores, "
    "extractions, vacancy_sources, vacancies, source_checks, raw_items, fetch_runs, source_state, "
    "llm_ledger, llm_budget, job_runs, app_state, profile_versions, scoring_versions RESTART IDENTITY CASCADE"
)


@pytest.fixture
async def aengine(test_engine, test_settings):
    """Асинхронное подключение к тестовой базе; перед тестом все таблицы пусты."""
    from hunter_core.storage.db import create_async_db_engine

    with test_engine.begin() as conn:
        conn.execute(text(ALL_APP_TABLES_SQL))
    engine = create_async_db_engine(test_settings, application_name="pytest-async")
    yield engine
    await engine.dispose()
