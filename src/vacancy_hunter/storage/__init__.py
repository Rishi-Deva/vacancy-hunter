"""Таблицы вакансий. Импорт модуля регистрирует все 20 таблиц в общем наборе `metadata`."""

from vacancy_hunter.storage import tables
from vacancy_hunter.storage.tables import ALL_TABLES

__all__ = ["ALL_TABLES", "tables"]
