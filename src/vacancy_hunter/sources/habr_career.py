"""Хабр Карьера через RSS вакансий (SOURCES.md, раздел 5.3).

Способ выбран на шаге A3: лента отвечает без ключа и входа. API требует ключ приложения,
условия его получения не проверены. В записи ленты: название, компания (author), дата,
ссылка и краткое описание (формат работы, навыки, уровень). Краткое описание
используется как текст вакансии; страницы вакансий не разбираются (правило R3).
"""

from __future__ import annotations

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


class HabrCareerAdapter:
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
                external_id = entry.get("guid") or ""
                link = entry.get("link", "")
                if not external_id.isdigit():
                    external_id = link.rstrip("/").rsplit("/", 1)[-1]
                if not external_id.isdigit():
                    continue
                published = parse_date(entry.get("pubDate", ""))
                if ctx.too_old(published):
                    continue
                items.setdefault(external_id, RawItem(
                    external_id=external_id, url=link, published_at=published,
                    payload={"title": entry.get("title", ""), "company": entry.get("author", ""), "link": link,
                             "description": entry.get("description", ""), "pubDate": entry.get("pubDate", ""),
                             "query": query},
                ))
        newest = max((i.published_at for i in items.values() if i.published_at), default=None)
        old = (cursor or {}).get("newest")
        new_cursor = {"newest": max(filter(None, [old, newest.isoformat() if newest else None]), default=None)}
        return FetchResult(items=list(items.values()), cursor=new_cursor, http_requests=len(self.queries),
                           seen_on_pages=seen)


@register("habr_career_rss")
def make_habr(entry, defaults) -> HabrCareerAdapter:
    return HabrCareerAdapter(entry.code, entry.url, list(entry.queries), pause_seconds=defaults.domain_pause_seconds)
