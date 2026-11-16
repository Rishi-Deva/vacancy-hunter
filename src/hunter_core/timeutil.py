"""Текущее время. Всё хранится в UTC; часы подменяются в тестах (TEST_PLAN.md, раздел 2)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)
