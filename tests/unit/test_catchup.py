"""JOB-3, JOB-4 (правило): догоняющий запуск отчёта после перезапуска (ARCHITECTURE.md, раздел 12)."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from hunter_core.scheduler import catchup_due

TBILISI = ZoneInfo("Asia/Tbilisi")


def at(hh, mm=0):
    return datetime(2026, 11, 13, hh, mm, tzinfo=TBILISI)


@pytest.mark.parametrize("now,done,expected", [
    (at(9), False, True),       # JOB-3: 09:00, отчёта нет — запустить сразу
    (at(15), False, False),     # JOB-4: 15:00 — ждать следующего дня
    (at(7), False, True),       # ровно время отчёта
    (at(6, 59), False, False),  # до времени отчёта — обычное расписание
    (at(13, 59), False, True),
    (at(14), False, False),     # предел догона не включается
    (at(9), True, False),       # отчёт уже есть
])
def test_catchup_window(now, done, expected):
    assert catchup_due(now, time(7, 0), time(14, 0), done) is expected
