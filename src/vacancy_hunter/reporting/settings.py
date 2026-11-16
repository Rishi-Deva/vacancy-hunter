"""Настройки, изменяемые из Telegram (TECHNICAL_SPEC.md, разделы 4.2, 16.5).

Хранятся в `app_state` и имеют приоритет над значениями файлов. Значение вне диапазона
отклоняется с указанием границ, прежнее сохраняется. Новые порог и число карточек
действуют со следующего отчёта и не меняют статусы ранее сохранённых вакансий.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from hunter_core.storage.tables import app_state
from vacancy_hunter.config import AppConfig
from vacancy_hunter.config.schemas import MAX_CARDS_LIMIT

KEYS = {"threshold": "settings.threshold", "max_cards": "settings.max_cards", "report_time": "report_time",
        "paused": "settings.paused"}
THRESHOLD_RANGE = (50, 90)
CARDS_RANGE = (1, MAX_CARDS_LIMIT)


class SettingError(ValueError):
    """Недопустимое значение: текст сообщения показывается владельцу."""


@dataclass(frozen=True)
class Settings:
    threshold: int
    max_cards: int
    report_time: str
    paused: bool


def defaults(config: AppConfig) -> Settings:
    r = config.scoring.report
    return Settings(r.threshold, r.max_cards, config.schedule.report_time, False)


def parse(name: str, raw: str) -> int | str | bool:
    raw = (raw or "").strip()
    if name == "threshold":
        lo, hi = THRESHOLD_RANGE
        if not re.fullmatch(r"\d{1,3}", raw) or not lo <= int(raw) <= hi:
            raise SettingError(f"Порог показа — целое число от {lo} до {hi}. Прежнее значение сохранено.")
        return int(raw)
    if name == "max_cards":
        lo, hi = CARDS_RANGE
        if not re.fullmatch(r"\d{1,3}", raw) or not lo <= int(raw) <= hi:
            raise SettingError(f"Число карточек — целое число от {lo} до {hi}. Прежнее значение сохранено.")
        return int(raw)
    if name == "report_time":
        m = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", raw)
        if not m or int(m.group(2)) % 5:
            raise SettingError("Время отчёта — ЧЧ:ММ с шагом 5 минут, например 07:00 или 08:35. "
                               "Прежнее значение сохранено.")
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if name == "paused":
        if raw.lower() in ("on", "1", "true", "да", "вкл"):
            return True
        if raw.lower() in ("off", "0", "false", "нет", "выкл"):
            return False
        raise SettingError("Пауза — «вкл» или «выкл».")
    raise SettingError(f"Неизвестная настройка {name}")


def confirmation(name: str, value) -> str:
    if name == "threshold":
        return f"Порог показа: {value}. Действует со следующего отчёта"
    if name == "max_cards":
        return f"Карточек в отчёте: {value}"
    if name == "report_time":
        return f"Отчёт будет приходить в {value} по времени Грузии"
    return "Отчёты приостановлены. Сбор и обработка продолжаются" if value else "Отчёты возобновлены"


def _valid_stored(name: str, value) -> bool:
    try:
        parse(name, "on" if value is True else "off" if value is False else str(value))
        return True
    except SettingError:
        return False


async def load_settings(source: AsyncEngine | AsyncConnection, config: AppConfig) -> Settings:
    if isinstance(source, AsyncEngine):
        async with source.connect() as conn:
            return await load_settings(conn, config)
    rows = dict((await source.execute(select(app_state.c.key, app_state.c.value)
                                      .where(app_state.c.key.in_(KEYS.values())))).all())
    base = defaults(config)
    values = {}
    for name, key in KEYS.items():
        value = rows.get(key)
        values[name] = value if value is not None and _valid_stored(name, value) else getattr(base, name)
    return Settings(**values)


async def save_setting(conn: AsyncConnection, name: str, value, now: datetime) -> None:
    key = KEYS[name]
    await conn.execute(pg_insert(app_state).values(key=key, value=value, updated_at=now)
                       .on_conflict_do_update(index_elements=[app_state.c.key],
                                              set_={"value": value, "updated_at": now}))
