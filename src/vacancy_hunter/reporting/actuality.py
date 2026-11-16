"""Проверка актуальности отобранных вакансий перед отчётом (TECHNICAL_SPEC.md, 14.3, 14.4).

- проверяются только отобранные, не более 20 запросов, общий предел 2 минуты;
- Telegram: повторный запрос поста `t.me/<канал>/<номер>?embed=1` (способ выбран на шаге A7):
  удалённый пост отдаётся со страницей ошибки `tgme_widget_message_error` → «закрыта»;
- RSS: проверка не поддерживается — «неизвестно» без пометки в карточке;
- срок приёма откликов истёк → «закрыта»; истекает в течение 2 дней → пометка в карточке;
- ошибка проверки не задерживает отчёт: вакансия показывается с пометкой «актуальность не проверена».
"""

from __future__ import annotations

import asyncio
import logging
import time as systime
from dataclasses import dataclass
from datetime import date, timedelta

from hunter_core.http import HttpClient

log = logging.getLogger("vacancy_hunter.actuality")

OPEN, CLOSED, UNKNOWN, UNSUPPORTED = "open", "closed", "unknown", "unsupported"
NOT_CHECKED_NOTE = "актуальность не проверена"
MAX_REQUESTS = 20
TOTAL_SECONDS = 120.0


@dataclass
class Check:
    state: str
    note: str | None = None
    reason: str | None = None


def deadline_check(deadline: str | None, today: date) -> Check | None:
    if not deadline:
        return None
    try:
        until = date.fromisoformat(deadline)
    except ValueError:
        return None
    if until < today:
        return Check(CLOSED, reason=f"срок приёма откликов истёк {until:%d.%m.%Y}")
    if until <= today + timedelta(days=2):
        return Check(OPEN, note=f"срок отклика до {until:%d.%m.%Y}")
    return None


class ActualityChecker:
    def __init__(self, http: HttpClient | None, *, max_requests: int = MAX_REQUESTS,
                 total_seconds: float = TOTAL_SECONDS):
        self.http = http
        self.max_requests = max_requests
        self.total_seconds = total_seconds
        self.requests = 0
        self._started: float | None = None

    def _time_left(self) -> float:
        if self._started is None:
            self._started = systime.monotonic()
        return self.total_seconds - (systime.monotonic() - self._started)

    async def check(self, candidate: dict, today: date) -> Check:
        by_deadline = deadline_check(candidate.get("deadline"), today)
        if by_deadline is not None and by_deadline.state == CLOSED:
            return by_deadline
        note = by_deadline.note if by_deadline else None
        post = candidate.get("telegram")
        if not post:
            return Check(UNSUPPORTED, note=note)
        result = await self._telegram(post)
        if result.state == UNKNOWN:
            result.note = "; ".join(n for n in (note, NOT_CHECKED_NOTE) if n)
        elif note:
            result.note = note
        return result

    async def _telegram(self, post: str) -> Check:
        left = self._time_left()
        if self.http is None or self.requests >= self.max_requests or left <= 0:
            return Check(UNKNOWN)
        self.requests += 1
        try:
            async with asyncio.timeout(left):
                response = await self.http.get(f"https://t.me/{post}", params={"embed": "1", "mode": "tme"})
        except Exception as exc:  # noqa: BLE001 — любая ошибка: «неизвестно», отчёт не задерживается
            log.info("actuality_check_failed", extra={"post": post, "error": type(exc).__name__})
            return Check(UNKNOWN)
        body = response.text if response.status_code == 200 else ""
        if "tgme_widget_message_error" in body:
            return Check(CLOSED, reason="пост удалён")
        if "tgme_widget_message_text" in body:
            return Check(OPEN)
        return Check(UNKNOWN)


__all__ = ["CLOSED", "NOT_CHECKED_NOTE", "OPEN", "UNKNOWN", "UNSUPPORTED", "ActualityChecker", "Check",
           "deadline_check"]
