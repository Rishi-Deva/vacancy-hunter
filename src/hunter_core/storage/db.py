"""Создание подключения к PostgreSQL: обычного (команды, миграции) и асинхронного (процесс приложения)."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from hunter_core.config.secrets import DatabaseSettings


def create_db_engine(settings: DatabaseSettings, *, application_name: str = "hunter") -> Engine:
    return create_engine(
        settings.url(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "application_name": application_name},
    )


def create_async_db_engine(settings: DatabaseSettings, *, application_name: str = "hunter") -> AsyncEngine:
    return create_async_engine(
        settings.url(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": 10, "application_name": application_name},
    )
