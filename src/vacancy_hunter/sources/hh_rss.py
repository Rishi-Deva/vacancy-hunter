"""hh.ru и hh.kz через RSS поиска (SOURCES.md, раздел 5.1; решение D1).

В ленте нет полного описания: записи обрабатываются в ограниченном режиме
(оценка по заголовку, шаг A4/A6). Страницы вакансий не разбираются (правило R3).
Запросы задаются в конфигурации; курсор — время самой свежей записи.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus, urlsplit

from hunter_core.sources import (
    FetchContext,
    FetchResult,
    RawItem,
    SourceTemporaryError,
    classify_status,
    register,
)
from vacancy_hunter.sources.rss import parse_date, parse_rss


class HhRssAdapter:
    kind = "rss"

    def __init__(self, code: str, url_template: str, queries: list[str], pause_seconds: float = 3.0):
        self.code = code
        self.url_template = url_template
        self.queries = queries or [""]
        self.pause_seconds = pause_seconds
        self.domains = (urlsplit(url_template).hostname or "",)

    def url_for(self, query: str) -> str:
        return self.url_template.replace("{query}", quote_plus(query))

    async def fetch(self, cursor: dict[str, Any] | None, ctx: FetchContext) -> FetchResult:
        items: dict[str, RawItem] = {}
        seen = 0
        for query in self.queries:
            try:
                response = await ctx.http.get(self.url_for(query))
            except Exception as exc:  # noqa: BLE001
                raise SourceTemporaryError(f"{self.code}: {type(exc).__name__}") from None
            classify_status(response.status_code, self.code)
            entries = parse_rss(response.text, self.code)
            seen += len(entries)
            for entry in entries:
                link = entry.get("link") or entry.get("guid") or ""
                match = re.search(r"/vacancy/(\d+)", link)
                if not match:
                    continue
                published = parse_date(entry.get("pubDate", ""))
                if ctx.too_old(published):
                    continue
                external_id = match.group(1)
                items.setdefault(external_id, RawItem(
                    external_id=external_id, url=link, published_at=published,
                    payload={"title": entry.get("title", ""), "link": link,
                             "description": entry.get("description", ""), "pubDate": entry.get("pubDate", ""),
                             "query": query},
                ))
        newest = max((i.published_at for i in items.values() if i.published_at), default=None)
        old = (cursor or {}).get("newest")
        new_cursor = {"newest": max(filter(None, [old, newest.isoformat() if newest else None]), default=None)}
        return FetchResult(items=list(items.values()), cursor=new_cursor, http_requests=len(self.queries),
                           seen_on_pages=seen)


@register("hh_rss")
def make_hh_rss(entry, defaults) -> HhRssAdapter:
    return HhRssAdapter(entry.code, entry.url, list(entry.queries), pause_seconds=defaults.domain_pause_seconds)
