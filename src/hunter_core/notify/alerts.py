"""Оповещения владельцу об ошибках (ARCHITECTURE.md, раздел 16; TECHNICAL_SPEC.md, раздел 23).

- Не чаще одного сообщения на тип ошибки в час: ключ outbox «alert:<тип>:<час UTC>».
- Ночью (23:00–07:00 по местному времени) — без звука.
- Текст экранируется: в нём может быть внешний текст ошибки.
"""

from __future__ import annotations

import html
import logging
from datetime import time
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncEngine

from hunter_core.notify.outbox import enqueue
from hunter_core.timeutil import Clock, utcnow

log = logging.getLogger("hunter_core.alerts")

QUIET_FROM = time(23, 0)
QUIET_TO = time(7, 0)


def is_quiet(local: time) -> bool:
    return local >= QUIET_FROM or local < QUIET_TO


class Alerts:
    def __init__(self, engine: AsyncEngine, owner_chat_id: int | None, *, timezone: str,
                 clock: Clock = utcnow, wake=None):
        self.engine = engine
        self.owner_chat_id = owner_chat_id
        self.tz = ZoneInfo(timezone)
        self.clock = clock
        self._wake = wake

    async def send(self, alert_type: str, text: str) -> bool:
        """Поставить оповещение в очередь. False — такое уже было в этот час или владелец не задан."""
        if self.owner_chat_id is None:
            log.warning("alert_not_sent_no_owner", extra={"alert_type": alert_type})
            return False
        now = self.clock()
        local = now.astimezone(self.tz).timetz().replace(tzinfo=None)
        payload = {
            "text": f"⚠️ {html.escape(text)[:3500]}",
            "parse_mode": "HTML",
            "disable_notification": is_quiet(local),
            "link_preview_options": {"is_disabled": True},
        }
        key = f"alert:{alert_type}:{now:%Y%m%d%H}"
        async with self.engine.begin() as conn:
            created = await enqueue(conn, dedup_key=key, chat_id=self.owner_chat_id, kind="alert",
                                    payload=payload, clock=self.clock)
        if created is not None and self._wake is not None:
            self._wake()
        return created is not None

    async def job_failed(self, job_name: str, exc: BaseException) -> None:
        await self.send(f"job_failed:{job_name}", f"Задача «{job_name}» завершилась с ошибкой: "
                                                  f"{type(exc).__name__}: {exc}")
