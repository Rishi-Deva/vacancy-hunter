"""Подключение к базе и общие таблицы."""

from hunter_core.storage.db import create_db_engine
from hunter_core.storage.metadata import metadata

__all__ = ["create_db_engine", "metadata"]
