"""Интерфейс адаптера источника (ARCHITECTURE.md, раздел 6).

Конвейер не знает, с каким источником работает. Адаптер умеет:
- описать себя (код, тип доступа, домены, пауза между запросами);
- получить новые записи по курсору и вернуть новый курсор;
- проверить актуальность записи (необязательно, шаг A7).
Нормализацию записи в предметный формат выполняет пакет продукта.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from hunter_core.http import HttpClient


class SourceError(Exception):
    """Сбой сбора. Курсор не сдвигается."""


class SourceTemporaryError(SourceError):
    """Сетевая ошибка, 429 или 5xx после всех повторов: попробовать в следующий раз."""


class SourceBlocked(SourceError):
    """Явный запрет: 403, 451, капча, требование входа. Источник отключается до решения владельца (R6)."""


class SourceParseError(SourceError):
    """Ответ получен, но не разобран: вероятно, изменилась разметка."""


@dataclass(frozen=True)
class RawItem:
    external_id: str
    url: str | None
    published_at: datetime | None
    payload: dict[str, Any]


@dataclass
class FetchResult:
    items: list[RawItem]
    cursor: dict[str, Any] | None
    http_requests: int = 0
    seen_on_pages: int = 0  # записей на страницах до отсечения по курсору и возрасту


@dataclass(frozen=True)
class FetchContext:
    http: HttpClient
    now: datetime
    max_age: timedelta
    log_extra: dict[str, Any] = field(default_factory=dict)

    def too_old(self, published_at: datetime | None) -> bool:
        return published_at is not None and published_at < self.now - self.max_age


class SourceAdapter(Protocol):
    code: str
    kind: str                       # telegram, rss, api
    domains: tuple[str, ...]
    pause_seconds: float

    async def fetch(self, cursor: dict[str, Any] | None, ctx: FetchContext) -> FetchResult: ...


AdapterFactory = Callable[..., SourceAdapter]


def classify_status(status: int, where: str) -> None:
    """Перевести код ответа в исключение сбора. 2xx — ничего не делать."""
    if 200 <= status < 300:
        return
    if status in (401, 403, 451):
        raise SourceBlocked(f"{where}: доступ запрещён (HTTP {status})")
    if status == 429 or status >= 500:
        raise SourceTemporaryError(f"{where}: временная ошибка (HTTP {status})")
    raise SourceError(f"{where}: неожиданный ответ (HTTP {status})")
