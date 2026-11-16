"""Окружение Alembic.

Адрес базы:
  - в тестах передаётся через config.attributes["url"] (тестовая база);
  - иначе собирается из DB_MIGRATOR_* — эти переменные есть только у сервиса migrate.
Пароль не выводится в журнал.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from hunter_core.config import read_database_settings
from hunter_core.storage.metadata import metadata
import vacancy_hunter.storage  # noqa: F401 — регистрирует все 20 таблиц в metadata

config = context.config
if config.config_file_name is not None and not config.attributes.get("skip_logging_setup"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = metadata


def _url():
    url = config.attributes.get("url")
    if url is not None:
        return url
    settings = read_database_settings(
        os.environ, user_var="DB_MIGRATOR_USER", password_var="DB_MIGRATOR_PASSWORD"
    )
    return settings.url()


def run_migrations_online() -> None:
    engine = create_engine(_url(), connect_args={"connect_timeout": 10, "application_name": "migrate"})
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Режим offline не используется: миграции применяются только к базе.")
run_migrations_online()
