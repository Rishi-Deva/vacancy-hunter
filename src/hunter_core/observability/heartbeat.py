"""Внешний пульс (ARCHITECTURE.md, разделы 11 и 16).

Раз в 15 минут — запрос на адрес сервиса-сторожа. Сторож оповещает владельца, если
сигнала нет 30 минут. Если адрес не задан, пульс отключён: остальное работает.
Адрес может содержать секретный ключ, поэтому в журнал пишется только домен.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from hunter_core.http import HttpClient, HttpPolicy

log = logging.getLogger("hunter_core.heartbeat")


class Heartbeat:
    def __init__(self, url: str | None, *, user_agent: str, transport: httpx.AsyncBaseTransport | None = None):
        self.url = (url or "").strip() or None
        self._http = None
        if self.url:
            host = urlsplit(self.url).hostname or ""
            self._http = HttpClient([host], user_agent=user_agent, transport=transport,
                                    policy=HttpPolicy(max_retries=1),
                                    safe_url=lambda u: f"{urlsplit(u).scheme}://{urlsplit(u).hostname}/***")

    @property
    def enabled(self) -> bool:
        return self.url is not None

    async def beat(self) -> bool:
        if not self.enabled:
            return False
        try:
            response = await self._http.get(self.url)
        except httpx.HTTPError as exc:
            log.warning("heartbeat_failed", extra={"error": type(exc).__name__})
            return False
        ok = response.status_code < 400
        (log.info if ok else log.warning)("heartbeat", extra={"status": response.status_code})
        return ok

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
