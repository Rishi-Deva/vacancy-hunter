"""Адаптеры конкретных источников. Импорт модулей регистрирует их в реестре hunter_core."""

from vacancy_hunter.sources import habr_career, hh_rss, telegram_preview  # noqa: F401
from vacancy_hunter.sources.factory import build_adapters

__all__ = ["build_adapters"]
