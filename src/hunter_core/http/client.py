"""HTTP-клиент (ARCHITECTURE.md, разделы 16 и 17).

- Таймауты: соединение 10 с, ответ 30 с.
- Повторы: до 3 раз с нарастающей паузой, только для сетевых ошибок и кодов 429 и 5xx.
  Остальные коды (например, 403, 404) не повторяются: решение принимает вызывающий код.
- Пауза между запросами к одному домену — своя для каждого домена.
- Запросы идут только на объявленные домены (и их поддомены); остальные отклоняются до отправки.
- В журнал пишется адрес без параметров запроса, код ответа и длительность.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("hunter_core.http")
# Библиотека httpx сама пишет полный адрес каждого запроса (вместе с токеном бота и параметрами).
# Свой журнал запросов ведём здесь, без секретов, поэтому её сообщения уровня INFO отключены.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class DomainNotAllowed(Exception):
    """Запрос на домен, который не объявлен."""


@dataclass(frozen=True)
class HttpPolicy:
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_retries: int = 3
    backoff_base: float = 2.0          # пауза перед повтором: 2, 4, 8 секунд
    backoff_max: float = 60.0
    default_domain_pause: float = 0.0  # пауза между запросами к домену, секунды
    domain_pauses: dict[str, float] = field(default_factory=dict)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _safe_url(url: str) -> str:
    """Адрес без параметров запроса: в них могут быть ключи доступа."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


class HttpClient:
    def __init__(
        self,
        allowed_domains: Iterable[str],
        *,
        user_agent: str,
        policy: HttpPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        safe_url: Callable[[str], str] = _safe_url,
    ):
        self.allowed = {d.lower().strip(".") for d in allowed_domains}
        self.policy = policy or HttpPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._safe_url = safe_url
        self._last_request: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(self.policy.read_timeout, connect=self.policy.connect_timeout),
            headers={"User-Agent": user_agent},
            follow_redirects=False,
        )

    def is_allowed(self, url: str) -> bool:
        host = _host(url)
        return any(host == d or host.endswith("." + d) for d in self.allowed)

    def _pause_for(self, host: str) -> float:
        for domain, pause in self.policy.domain_pauses.items():
            if host == domain or host.endswith("." + domain):
                return pause
        return self.policy.default_domain_pause

    async def _wait_domain_pause(self, host: str) -> None:
        pause = self._pause_for(host)
        last = self._last_request.get(host)
        if pause > 0 and last is not None:
            remaining = pause - (self._monotonic() - last)
            if remaining > 0:
                await self._sleep(remaining)
        self._last_request[host] = self._monotonic()

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None and response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return min(float(retry_after), self.policy.backoff_max)
        return min(self.policy.backoff_base * 2 ** (attempt - 1), self.policy.backoff_max)

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Выполнить запрос с повторами. Возвращает последний ответ; сетевая ошибка — исключение."""
        if not self.is_allowed(url):
            log.warning("http_domain_not_allowed", extra={"url": self._safe_url(url)})
            raise DomainNotAllowed(f"домен не объявлен: {_host(url)}")
        host = _host(url)
        lock = self._locks.setdefault(host, asyncio.Lock())
        attempt = 0
        while True:
            attempt += 1
            async with lock:
                await self._wait_domain_pause(host)
                started = self._monotonic()
                try:
                    response = await self._client.request(method, url, **kwargs)
                except httpx.TransportError as exc:
                    log.warning("http_error", extra={"url": self._safe_url(url), "error": type(exc).__name__,
                                                     "attempt": attempt})
                    if attempt > self.policy.max_retries:
                        raise
                    response = None
                    error = exc
                else:
                    error = None
                    log.info("http_request", extra={
                        "url": self._safe_url(url), "status": response.status_code, "attempt": attempt,
                        "duration_ms": round((self._monotonic() - started) * 1000),
                    })
            if error is None and (response.status_code not in RETRY_STATUSES or attempt > self.policy.max_retries):
                return response
            await self._sleep(self._backoff(attempt, response))

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
